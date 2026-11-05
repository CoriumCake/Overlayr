//! The debug view's Resources tab: Overlayr's own CPU, memory, disk and GPU
//! use over the last minute, laid out like Task Manager's Performance page.

use eframe::egui::{
    self, Color32, CornerRadius, Margin, Pos2, Rect, RichText, Sense, Shape, Stroke, StrokeKind,
    Vec2,
};

use crate::resources::{HISTORY_LENGTH, ResourceMonitor, ResourceUsage, format_bytes, format_rate};

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum Resource {
    #[default]
    Cpu,
    Memory,
    Disk,
    Gpu,
}

impl Resource {
    const ALL: [Self; 4] = [Self::Cpu, Self::Memory, Self::Disk, Self::Gpu];

    fn label(self) -> &'static str {
        match self {
            Self::Cpu => "CPU",
            Self::Memory => "Memory",
            Self::Disk => "Disk",
            Self::Gpu => "GPU",
        }
    }

    fn color(self) -> Color32 {
        match self {
            Self::Cpu => Color32::from_rgb(0x17, 0x8f, 0xe0),
            Self::Memory => Color32::from_rgb(0x9b, 0x5c, 0xe0),
            Self::Disk => Color32::from_rgb(0x4c, 0xb0, 0x50),
            Self::Gpu => Color32::from_rgb(0xe0, 0x8a, 0x1e),
        }
    }

    /// The headline value for the list on the left.
    fn summary(self, usage: &ResourceUsage) -> String {
        match self {
            Self::Cpu => format!("{:.0}%", usage.system_cpu_percent),
            Self::Memory => format_bytes(usage.memory_bytes),
            Self::Disk => format_rate(usage.disk_read_rate + usage.disk_write_rate),
            Self::Gpu => usage
                .gpu_percent
                .map_or_else(|| "n/a".into(), |percent| format!("{percent:.0}%")),
        }
    }

    /// The graphed series, in display units.
    fn series(self, usage: &ResourceUsage) -> Vec<f64> {
        match self {
            Self::Cpu => vec![usage.system_cpu_percent as f64],
            Self::Memory => vec![usage.memory_bytes as f64],
            Self::Disk => vec![usage.disk_read_rate, usage.disk_write_rate],
            Self::Gpu => vec![usage.gpu_percent.unwrap_or(0.0) as f64],
        }
    }
}

pub fn resources_ui(ui: &mut egui::Ui, monitor: &ResourceMonitor, selected: &mut Resource) {
    if !ResourceMonitor::supported() {
        ui.label(RichText::new("Resource usage isn't available on this platform.").weak());
        return;
    }
    let history = monitor.history();
    let Some(latest) = monitor.latest() else {
        ui.label(RichText::new("Measuring…").weak());
        return;
    };
    ui.label(
        RichText::new("Only Overlayr's own process is measured, not the rest of the PC.")
            .small()
            .weak(),
    );
    ui.add_space(6.0);

    let narrow = ui.available_width() < 560.0;
    let list = |ui: &mut egui::Ui, selected: &mut Resource| {
        for resource in Resource::ALL {
            resource_tile(ui, resource, history, latest, selected);
        }
    };
    if narrow {
        ui.horizontal_wrapped(|ui| list(ui, selected));
        ui.add_space(8.0);
        detail(ui, *selected, history, latest);
    } else {
        ui.horizontal_top(|ui| {
            ui.allocate_ui_with_layout(
                Vec2::new(190.0, ui.available_height()),
                egui::Layout::top_down(egui::Align::Min),
                |ui| list(ui, selected),
            );
            ui.add_space(8.0);
            ui.vertical(|ui| detail(ui, *selected, history, latest));
        });
    }
}

/// One entry of the list: name, current value and a small graph.
fn resource_tile(
    ui: &mut egui::Ui,
    resource: Resource,
    history: &std::collections::VecDeque<ResourceUsage>,
    latest: &ResourceUsage,
    selected: &mut Resource,
) {
    let active = *selected == resource;
    let fill = if active {
        ui.visuals().selection.bg_fill.gamma_multiply(0.35)
    } else {
        ui.visuals().faint_bg_color
    };
    let response = egui::Frame::new()
        .fill(fill)
        .stroke(if active {
            Stroke::new(1.0_f32, resource.color())
        } else {
            ui.visuals().widgets.noninteractive.bg_stroke
        })
        .corner_radius(CornerRadius::same(6))
        .inner_margin(Margin::same(8))
        .show(ui, |ui| {
            ui.set_width(174.0);
            ui.horizontal(|ui| {
                let (rect, _) = ui.allocate_exact_size(Vec2::new(64.0, 36.0), Sense::hover());
                graph(ui, rect, resource, history, false);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    ui.label(RichText::new(resource.label()).strong());
                    ui.label(RichText::new(resource.summary(latest)).small());
                });
            });
        })
        .response
        .interact(Sense::click());
    if response.clicked() {
        *selected = resource;
    }
    if response.hovered() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
}

/// The selected resource: big graph, figures and, for CPU, its threads.
fn detail(
    ui: &mut egui::Ui,
    resource: Resource,
    history: &std::collections::VecDeque<ResourceUsage>,
    latest: &ResourceUsage,
) {
    // Leave room for the scroll bar, which floats over the right edge.
    ui.set_max_width(ui.available_width() - 14.0);
    ui.horizontal(|ui| {
        ui.heading(resource.label());
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(RichText::new("Overlayr").weak());
        });
    });
    let (caption, max) = scale(resource, history);
    ui.horizontal(|ui| {
        ui.label(RichText::new(caption).small().weak());
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(RichText::new(max).small().weak());
        });
    });
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(Vec2::new(width, 220.0), Sense::hover());
    graph(ui, rect, resource, history, true);
    ui.horizontal(|ui| {
        ui.label(RichText::new("60 seconds").small().weak());
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(RichText::new("0").small().weak());
        });
    });
    ui.add_space(8.0);

    let figures: Vec<(&str, String)> = match resource {
        Resource::Cpu => vec![
            ("Utilization", format!("{:.1}%", latest.system_cpu_percent)),
            (
                "Cores busy",
                format!("{:.1} of {}", latest.cpu_percent / 100.0, cores()),
            ),
            (
                "Average (1 min)",
                format!("{:.1}%", average(history, |u| u.system_cpu_percent as f64)),
            ),
            (
                "Peak (1 min)",
                format!("{:.1}%", peak(history, |u| u.system_cpu_percent as f64)),
            ),
            ("Threads", latest.threads.to_string()),
        ],
        Resource::Memory => vec![
            ("In use", format_bytes(latest.memory_bytes)),
            ("Peak since launch", format_bytes(latest.peak_memory_bytes)),
            ("Reserved (virtual)", format_bytes(latest.virtual_bytes)),
        ],
        Resource::Disk => vec![
            ("Read speed", format_rate(latest.disk_read_rate)),
            ("Write speed", format_rate(latest.disk_write_rate)),
            (
                "Read (1 min)",
                format_bytes(history.iter().map(|u| u.disk_read_rate).sum::<f64>() as u64),
            ),
            (
                "Written (1 min)",
                format_bytes(history.iter().map(|u| u.disk_write_rate).sum::<f64>() as u64),
            ),
        ],
        Resource::Gpu => match latest.gpu_percent {
            Some(percent) => vec![
                ("Utilization", format!("{percent:.0}%")),
                (
                    "Dedicated memory",
                    format_bytes(latest.gpu_dedicated_bytes.unwrap_or(0)),
                ),
                (
                    "Shared memory",
                    format_bytes(latest.gpu_shared_bytes.unwrap_or(0)),
                ),
            ],
            None => vec![("Utilization", "Not reported by the GPU driver".into())],
        },
    };
    egui::Grid::new(("resource_figures", resource.label()))
        .num_columns(2)
        .spacing([24.0, 4.0])
        .show(ui, |ui| {
            for (label, value) in figures {
                ui.label(RichText::new(label).weak());
                ui.label(RichText::new(value).strong());
                ui.end_row();
            }
        });

    if resource == Resource::Disk {
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            legend(ui, Resource::Disk.color(), "Read");
            legend(ui, write_color(), "Write");
        });
    }
    if resource == Resource::Cpu {
        ui.add_space(8.0);
        ui.label(RichText::new("CPU by thread").strong());
        egui::Grid::new("resource_threads")
            .num_columns(3)
            .striped(true)
            .spacing([16.0, 4.0])
            .show(ui, |ui| {
                for group in &latest.thread_groups {
                    ui.label(&group.name);
                    ui.label(
                        RichText::new(match group.count {
                            0 => String::new(),
                            1 => "1 thread".into(),
                            count => format!("{count} threads"),
                        })
                        .small()
                        .weak(),
                    );
                    ui.label(format!("{:.1}%", group.cpu_percent / cores() as f32));
                    ui.end_row();
                }
            });
    }
}

fn cores() -> usize {
    std::thread::available_parallelism().map_or(1, |cores| cores.get())
}

fn legend(ui: &mut egui::Ui, color: Color32, label: &str) {
    ui.label(RichText::new("■").color(color));
    ui.label(RichText::new(label).small());
    ui.add_space(6.0);
}

fn write_color() -> Color32 {
    Color32::from_rgb(0xa5, 0xd6, 0x7a)
}

/// The graph's top value: 100% for GPU, otherwise the minute's peak rounded
/// up, so small numbers don't flatline. CPU starts at one full core.
fn graph_max(resource: Resource, history: &std::collections::VecDeque<ResourceUsage>) -> f64 {
    let highest = history
        .iter()
        .flat_map(|usage| resource.series(usage))
        .fold(0.0, f64::max);
    match resource {
        // Like Task Manager: the top is every core fully busy.
        Resource::Gpu | Resource::Cpu => 100.0,
        Resource::Memory => nice_ceiling(highest.max(64.0 * 1024.0 * 1024.0)),
        Resource::Disk => nice_ceiling(highest.max(1024.0 * 1024.0)),
    }
}

fn scale(
    resource: Resource,
    history: &std::collections::VecDeque<ResourceUsage>,
) -> (&'static str, String) {
    let max = graph_max(resource, history);
    match resource {
        Resource::Cpu => ("% utilization of all cores", format!("{max:.0}%")),
        Resource::Memory => ("Memory in use", format_bytes(max as u64)),
        Resource::Disk => ("Disk transfer rate", format_rate(max)),
        Resource::Gpu => ("% utilization (busiest engine)", "100%".into()),
    }
}

/// Rounds up to 1, 2 or 5 times a power of ten.
fn nice_ceiling(value: f64) -> f64 {
    if value <= 0.0 {
        return 1.0;
    }
    let magnitude = 10f64.powf(value.log10().floor());
    [1.0, 2.0, 5.0, 10.0]
        .into_iter()
        .map(|step| step * magnitude)
        .find(|candidate| *candidate >= value)
        .unwrap_or(10.0 * magnitude)
}

fn average(
    history: &std::collections::VecDeque<ResourceUsage>,
    value: impl Fn(&ResourceUsage) -> f64,
) -> f64 {
    if history.is_empty() {
        return 0.0;
    }
    history.iter().map(value).sum::<f64>() / history.len() as f64
}

fn peak(
    history: &std::collections::VecDeque<ResourceUsage>,
    value: impl Fn(&ResourceUsage) -> f64,
) -> f64 {
    history.iter().map(value).fold(0.0, f64::max)
}

/// Draws the last minute right-aligned, newest at the right edge, as filled
/// lines over a grid.
fn graph(
    ui: &egui::Ui,
    rect: Rect,
    resource: Resource,
    history: &std::collections::VecDeque<ResourceUsage>,
    grid: bool,
) {
    let painter = ui.painter_at(rect);
    let color = resource.color();
    painter.rect_filled(rect, CornerRadius::same(2), ui.visuals().extreme_bg_color);
    if grid {
        let line = Stroke::new(1.0_f32, color.gamma_multiply(0.15));
        for row in 1..10 {
            let y = rect.top() + rect.height() * row as f32 / 10.0;
            painter.line_segment(
                [Pos2::new(rect.left(), y), Pos2::new(rect.right(), y)],
                line,
            );
        }
        for column in 1..20 {
            let x = rect.left() + rect.width() * column as f32 / 20.0;
            painter.line_segment(
                [Pos2::new(x, rect.top()), Pos2::new(x, rect.bottom())],
                line,
            );
        }
    }

    let max = graph_max(resource, history);
    let step = rect.width() / (HISTORY_LENGTH - 1) as f32;
    let offset = HISTORY_LENGTH - history.len();
    let colors = [color, write_color()];
    let series_count = history
        .back()
        .map_or(0, |usage| resource.series(usage).len());
    for series in 0..series_count {
        let points: Vec<Pos2> = history
            .iter()
            .enumerate()
            .map(|(index, usage)| {
                let value = resource.series(usage)[series];
                let fraction = (value / max).clamp(0.0, 1.0) as f32;
                Pos2::new(
                    rect.left() + (offset + index) as f32 * step,
                    rect.bottom() - fraction * rect.height(),
                )
            })
            .collect();
        let series_color = colors[series % colors.len()];
        // Each segment's area is a convex quad, which egui fills reliably.
        let fill = series_color.gamma_multiply(0.18);
        for pair in points.windows(2) {
            painter.add(Shape::convex_polygon(
                vec![
                    pair[0],
                    pair[1],
                    Pos2::new(pair[1].x, rect.bottom()),
                    Pos2::new(pair[0].x, rect.bottom()),
                ],
                fill,
                Stroke::NONE,
            ));
        }
        painter.add(Shape::line(points, Stroke::new(1.5_f32, series_color)));
    }
    painter.rect_stroke(
        rect,
        CornerRadius::same(2),
        Stroke::new(1.0_f32, color.gamma_multiply(0.6)),
        StrokeKind::Inside,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounds_graph_scale() {
        assert_eq!(nice_ceiling(100.0), 100.0);
        assert_eq!(nice_ceiling(130.0), 200.0);
        assert_eq!(nice_ceiling(301.0), 500.0);
        assert_eq!(nice_ceiling(600.0), 1000.0);
    }
}
