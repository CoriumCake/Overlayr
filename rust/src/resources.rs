//! How much CPU, memory, disk and GPU Overlayr itself uses, for debug mode.
//! Nothing here measures the rest of the machine.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

/// How often usage is re-read; rates are averaged over this window.
const SAMPLE_INTERVAL: Duration = Duration::from_secs(1);
/// Samples kept for the graphs: one minute, like Task Manager.
pub const HISTORY_LENGTH: usize = 60;

/// CPU use of the threads that share a name.
#[derive(Clone, Debug, PartialEq)]
pub struct ThreadGroup {
    pub name: String,
    pub count: usize,
    /// 100% is one fully busy core.
    pub cpu_percent: f32,
}

/// Overlayr's resource use over one sample interval.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ResourceUsage {
    /// CPU use like `top` shows it: 100% is one fully busy core.
    pub cpu_percent: f32,
    /// CPU use as a share of every core on the machine.
    pub system_cpu_percent: f32,
    /// Memory in RAM now, in bytes.
    pub memory_bytes: u64,
    /// The most memory in RAM since launch, in bytes.
    pub peak_memory_bytes: u64,
    /// Memory reserved, whether or not it is in RAM, in bytes.
    pub virtual_bytes: u64,
    pub threads: u32,
    /// Bytes per second read from and written to storage.
    pub disk_read_rate: f64,
    pub disk_write_rate: f64,
    /// Busiest GPU engine's use; `None` when the driver doesn't report it.
    pub gpu_percent: Option<f32>,
    /// GPU memory on the card, and system memory the GPU uses, in bytes.
    pub gpu_dedicated_bytes: Option<u64>,
    pub gpu_shared_bytes: Option<u64>,
    /// Busiest thread groups first; the last entry may be finished child
    /// processes such as the Tesseract CLI.
    pub thread_groups: Vec<ThreadGroup>,
}

/// Samples [`ResourceUsage`] at most once per [`SAMPLE_INTERVAL`] and keeps
/// the last [`HISTORY_LENGTH`] samples.
#[derive(Default)]
pub struct ResourceMonitor {
    last: Option<(ProcessStats, Instant)>,
    history: VecDeque<ResourceUsage>,
}

impl ResourceMonitor {
    /// Takes a sample if the last one is old. Cheap to call every frame.
    pub fn tick(&mut self) {
        let now = Instant::now();
        if self
            .last
            .as_ref()
            .is_some_and(|(_, at)| now.duration_since(*at) < SAMPLE_INTERVAL)
        {
            return;
        }
        let Some(stats) = read_process() else {
            return;
        };
        if let Some((previous, at)) = &self.last {
            let elapsed = now.duration_since(*at);
            if self.history.len() == HISTORY_LENGTH {
                self.history.pop_front();
            }
            self.history.push_back(usage(previous, &stats, elapsed));
        }
        self.last = Some((stats, now));
    }

    /// Samples, oldest first.
    pub fn history(&self) -> &VecDeque<ResourceUsage> {
        &self.history
    }

    pub fn latest(&self) -> Option<&ResourceUsage> {
        self.history.back()
    }

    /// Whether this platform can be measured at all.
    pub fn supported() -> bool {
        cfg!(any(target_os = "linux", target_os = "macos"))
    }
}

fn usage(previous: &ProcessStats, current: &ProcessStats, elapsed: Duration) -> ResourceUsage {
    let seconds = elapsed.as_secs_f32().max(f32::EPSILON);
    let percent = |before: Duration, after: Duration| {
        after.saturating_sub(before).as_secs_f32() / seconds * 100.0
    };
    let cpu_percent = percent(previous.cpu_time, current.cpu_time);
    let cores = std::thread::available_parallelism().map_or(1, |cores| cores.get());

    let mut groups: HashMap<String, ThreadGroup> = HashMap::new();
    for thread in &current.threads {
        let before = previous
            .threads
            .iter()
            .find(|old| old.id == thread.id)
            .map_or(Duration::ZERO, |old| old.cpu_time);
        let group = groups
            .entry(thread.name.clone())
            .or_insert_with(|| ThreadGroup {
                name: thread.name.clone(),
                count: 0,
                cpu_percent: 0.0,
            });
        group.count += 1;
        group.cpu_percent += percent(before, thread.cpu_time);
    }
    let mut thread_groups: Vec<ThreadGroup> = groups.into_values().collect();
    thread_groups.sort_by(|a, b| {
        b.cpu_percent
            .total_cmp(&a.cpu_percent)
            .then_with(|| a.name.cmp(&b.name))
    });
    let children = percent(previous.children_cpu_time, current.children_cpu_time);
    if children > 0.0 {
        thread_groups.push(ThreadGroup {
            name: "Child processes".into(),
            count: 0,
            cpu_percent: children,
        });
    }

    let rate = |before: u64, after: u64| after.saturating_sub(before) as f64 / seconds as f64;
    let gpu_percent = current.gpu.as_ref().map(|gpu| {
        let busy = gpu
            .engine_time
            .iter()
            .map(|(engine, time)| {
                let before = previous
                    .gpu
                    .as_ref()
                    .and_then(|old| old.engine_time.get(engine))
                    .copied()
                    .unwrap_or(*time);
                percent(before, *time)
            })
            .fold(0.0, f32::max);
        busy.min(100.0)
    });

    ResourceUsage {
        cpu_percent,
        system_cpu_percent: cpu_percent / cores as f32,
        memory_bytes: current.memory_bytes,
        peak_memory_bytes: current.peak_memory_bytes,
        virtual_bytes: current.virtual_bytes,
        threads: current.threads.len() as u32,
        disk_read_rate: rate(previous.disk_read, current.disk_read),
        disk_write_rate: rate(previous.disk_write, current.disk_write),
        gpu_percent,
        gpu_dedicated_bytes: current.gpu.as_ref().map(|gpu| gpu.dedicated_bytes),
        gpu_shared_bytes: current.gpu.as_ref().map(|gpu| gpu.shared_bytes),
        thread_groups,
    }
}

struct ThreadStats {
    id: u32,
    name: String,
    cpu_time: Duration,
}

struct GpuStats {
    /// Busy time per engine (gfx, compute, …), summed over GPU clients.
    engine_time: HashMap<String, Duration>,
    dedicated_bytes: u64,
    shared_bytes: u64,
}

struct ProcessStats {
    /// User and system time of every thread, finished child processes
    /// included.
    cpu_time: Duration,
    children_cpu_time: Duration,
    threads: Vec<ThreadStats>,
    memory_bytes: u64,
    peak_memory_bytes: u64,
    virtual_bytes: u64,
    disk_read: u64,
    disk_write: u64,
    gpu: Option<GpuStats>,
}

#[cfg(target_os = "linux")]
fn read_process() -> Option<ProcessStats> {
    use std::fs;

    let (own, children) = read_cpu_ticks(&fs::read_to_string("/proc/self/stat").ok()?)?;

    let mut threads = Vec::new();
    let own_id = std::process::id();
    for entry in fs::read_dir("/proc/self/task").ok()?.flatten() {
        let Some(id) = entry.file_name().to_str().and_then(|id| id.parse().ok()) else {
            continue;
        };
        let path = entry.path();
        // Threads can exit between listing and reading.
        let Some((ticks, _)) = fs::read_to_string(path.join("stat"))
            .ok()
            .and_then(|stat| read_cpu_ticks(&stat))
        else {
            continue;
        };
        let name = if id == own_id {
            "Main (UI)".to_owned()
        } else {
            fs::read_to_string(path.join("comm"))
                .map(|name| name.trim().to_owned())
                .unwrap_or_default()
        };
        threads.push(ThreadStats {
            id,
            name,
            cpu_time: ticks_to_duration(ticks),
        });
    }

    let status = fs::read_to_string("/proc/self/status").ok()?;
    let kilobytes = |key: &str| {
        status
            .lines()
            .find_map(|line| line.strip_prefix(key))
            .and_then(|value| {
                value
                    .trim()
                    .trim_end_matches("kB")
                    .trim()
                    .parse::<u64>()
                    .ok()
            })
            .unwrap_or(0)
            * 1024
    };

    let io = fs::read_to_string("/proc/self/io").unwrap_or_default();
    let io_bytes = |key: &str| {
        io.lines()
            .find_map(|line| line.strip_prefix(key))
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(0)
    };

    Some(ProcessStats {
        cpu_time: ticks_to_duration(own + children),
        children_cpu_time: ticks_to_duration(children),
        threads,
        memory_bytes: kilobytes("VmRSS:"),
        peak_memory_bytes: kilobytes("VmHWM:"),
        virtual_bytes: kilobytes("VmSize:"),
        disk_read: io_bytes("read_bytes:"),
        disk_write: io_bytes("write_bytes:"),
        gpu: read_gpu(),
    })
}

/// Own and finished-children CPU ticks from a `stat` file.
#[cfg(target_os = "linux")]
fn read_cpu_ticks(stat: &str) -> Option<(u64, u64)> {
    // The process name may contain spaces, so fields are counted from the
    // `)` that closes it; the state, field 3, comes first.
    let fields: Vec<&str> = stat.rsplit_once(')')?.1.split_whitespace().collect();
    let field = |number: usize| fields.get(number - 3)?.parse::<u64>().ok();
    Some((field(14)? + field(15)?, field(16)? + field(17)?))
}

#[cfg(target_os = "linux")]
fn ticks_to_duration(ticks: u64) -> Duration {
    // Linux reports CPU time in USER_HZ ticks, which is 100 on every
    // architecture it supports.
    Duration::from_millis(ticks * 10)
}

/// GPU use from the DRM `fdinfo` keys that amdgpu, i915, xe and other
/// drivers publish. One GPU client can be open through several files, so
/// clients are counted once.
#[cfg(target_os = "linux")]
fn read_gpu() -> Option<GpuStats> {
    let mut clients = std::collections::HashSet::new();
    let mut gpu = GpuStats {
        engine_time: HashMap::new(),
        dedicated_bytes: 0,
        shared_bytes: 0,
    };
    for entry in std::fs::read_dir("/proc/self/fdinfo").ok()?.flatten() {
        let Ok(info) = std::fs::read_to_string(entry.path()) else {
            continue;
        };
        let value = |key: &str| {
            info.lines()
                .find_map(|line| line.strip_prefix(key))
                .map(str::trim)
        };
        let (Some(driver), Some(client)) = (value("drm-driver:"), value("drm-client-id:")) else {
            continue;
        };
        if !clients.insert((driver.to_owned(), client.to_owned())) {
            continue;
        }
        for line in info.lines() {
            let Some((key, amount)) = line.split_once(':') else {
                continue;
            };
            let amount = amount.trim();
            if let Some(engine) = key.strip_prefix("drm-engine-")
                && let Some(nanoseconds) = amount
                    .strip_suffix("ns")
                    .and_then(|ns| ns.trim().parse::<u64>().ok())
            {
                *gpu.engine_time.entry(engine.to_owned()).or_default() +=
                    Duration::from_nanos(nanoseconds);
            }
        }
        let size = |key: &str| {
            let amount = value(key)?;
            let (number, unit) = amount.split_once(' ').unwrap_or((amount, ""));
            let number = number.parse::<u64>().ok()?;
            Some(match unit.trim() {
                "KiB" => number * 1024,
                "MiB" => number * 1024 * 1024,
                "GiB" => number * 1024 * 1024 * 1024,
                _ => number,
            })
        };
        gpu.dedicated_bytes += size("drm-total-vram:")
            .or_else(|| size("drm-memory-vram:"))
            .unwrap_or(0);
        gpu.shared_bytes += size("drm-total-gtt:")
            .or_else(|| size("drm-memory-gtt:"))
            .unwrap_or(0);
    }
    (!clients.is_empty()).then_some(gpu)
}

/// macOS has no per-thread names to show without the Mach thread APIs, so
/// only process totals are read; GPU use isn't exposed per process.
#[cfg(target_os = "macos")]
fn read_process() -> Option<ProcessStats> {
    use std::mem::MaybeUninit;

    let cpu_time = |who: libc::c_int| {
        let mut usage = MaybeUninit::<libc::rusage>::zeroed();
        // SAFETY: getrusage fills the rusage it is given.
        if unsafe { libc::getrusage(who, usage.as_mut_ptr()) } != 0 {
            return None;
        }
        // SAFETY: initialised by the successful call above.
        let usage = unsafe { usage.assume_init() };
        let time = |value: libc::timeval| {
            Duration::from_secs(value.tv_sec.max(0) as u64)
                + Duration::from_micros(value.tv_usec.max(0) as u64)
        };
        // ru_maxrss is in bytes on macOS (kilobytes on Linux).
        Some((
            time(usage.ru_utime) + time(usage.ru_stime),
            usage.ru_maxrss.max(0) as u64,
        ))
    };
    let (own, peak_memory_bytes) = cpu_time(libc::RUSAGE_SELF)?;
    let (children, _) = cpu_time(libc::RUSAGE_CHILDREN).unwrap_or_default();

    let pid = std::process::id() as libc::c_int;
    let mut usage = MaybeUninit::<libc::rusage_info_v2>::zeroed();
    // SAFETY: RUSAGE_INFO_V2 asks for exactly a rusage_info_v2, which the
    // function writes through the pointer it is given.
    let usage = (unsafe {
        libc::proc_pid_rusage(
            pid,
            libc::RUSAGE_INFO_V2,
            usage.as_mut_ptr().cast::<libc::rusage_info_t>(),
        )
    } == 0)
        // SAFETY: initialised by the successful call above.
        .then(|| unsafe { usage.assume_init() });

    let mut task = MaybeUninit::<libc::proc_taskinfo>::zeroed();
    let size = size_of::<libc::proc_taskinfo>() as libc::c_int;
    // SAFETY: PROC_PIDTASKINFO fills a proc_taskinfo of the given size.
    let task = (unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTASKINFO,
            0,
            task.as_mut_ptr().cast::<libc::c_void>(),
            size,
        )
    } == size)
        // SAFETY: fully written by the successful call above.
        .then(|| unsafe { task.assume_init() });

    Some(ProcessStats {
        cpu_time: own + children,
        children_cpu_time: children,
        threads: Vec::new(),
        memory_bytes: usage
            .map(|usage| usage.ri_resident_size)
            .or(task.map(|task| task.pti_resident_size))
            .unwrap_or(0),
        peak_memory_bytes,
        virtual_bytes: task.map_or(0, |task| task.pti_virtual_size),
        disk_read: usage.map_or(0, |usage| usage.ri_diskio_bytesread),
        disk_write: usage.map_or(0, |usage| usage.ri_diskio_byteswritten),
        gpu: None,
    })
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn read_process() -> Option<ProcessStats> {
    None
}

/// Formats a byte count as KB, MB or GB.
pub fn format_bytes(bytes: u64) -> String {
    let bytes = bytes as f64;
    if bytes >= 1024.0 * 1024.0 * 1024.0 {
        format!("{:.2} GB", bytes / (1024.0 * 1024.0 * 1024.0))
    } else if bytes >= 1024.0 * 1024.0 {
        format!("{:.0} MB", bytes / (1024.0 * 1024.0))
    } else {
        format!("{:.0} KB", bytes / 1024.0)
    }
}

/// Formats bytes per second.
pub fn format_rate(bytes_per_second: f64) -> String {
    format!("{}/s", format_bytes(bytes_per_second as u64))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_bytes() {
        assert_eq!(format_bytes(300 * 1024 * 1024), "300 MB");
        assert_eq!(format_bytes(1536 * 1024 * 1024), "1.50 GB");
        assert_eq!(format_rate(2048.0), "2 KB/s");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn reads_own_process() {
        let stats = read_process().expect("/proc/self is readable");
        assert!(stats.memory_bytes > 0);
        assert!(stats.peak_memory_bytes >= stats.memory_bytes);
        assert!(
            stats
                .threads
                .iter()
                .any(|thread| thread.name == "Main (UI)")
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn parses_stat_with_spaces_in_name() {
        let stat = "12 (my (odd) app) S 1 2 3 4 5 6 7 8 9 10 30 20 5 4 0 0 3 0";
        assert_eq!(read_cpu_ticks(stat), Some((50, 9)));
    }
}
