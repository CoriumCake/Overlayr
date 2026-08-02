================================================================================
  OVERLAYR — รายละเอียดฟังก์ชันทั้งหมด (ภาษาไทย)
  อัปเดตล่าสุด: 2026-08-03
================================================================================

โครงสร้างโปรเจกต์
─────────────────
src/overlayr/
  ├── core/
  │   ├── engine.py          — โค้ดหลักที่ควบคุม Pipeline การประมวลผล OCR และแปลภาษาแบบ Async (อัปเดตใหม่รองรับ AI Translate)
  │   ├── capture_engine.py  — ระบบดักจับภาพหน้าจอ / หน้าต่าง 
  │   ├── post_processing.py — ทำความสะอาดและแก้ไขข้อความหลังได้ผลลัพธ์จาก OCR
  │   └── wgc_capture.py     — การดักจับหน้าต่างเบื้องหลังด้วย Windows Graphics Capture (WGC) (มีการ Drain Pool เพื่อแก้ปัญหาภาพดีเลย์)
  │
  ├── services/
  │   ├── auth_service.py         — ระบบ Login / Session / Subscription
  │   ├── config_service.py       — โหลด/บันทึกการตั้งค่า (Settings) ในรูปแบบ JSON
  │   ├── deepseek_translate.py   — **[NEW]** บริการแปลภาษาด้วย DeepSeek V4 Flash API (OpenAI Compatible) รองรับ Batch Processing เร็วสูง
  │   ├── feature_gate.py         — ตรวจสิทธิ์การใช้งานฟีเจอร์ต่างๆ ตามระดับของผู้ใช้
  │   ├── gemini_translate.py     — **[NEW]** บริการแปลภาษาด้วย Google Gemini 1.5 Flash (แบบ Batch Array)
  │   ├── google_translate.py     — บริการแปลภาษาด้วย Google Translate (ตัวดั้งเดิม / Legacy)
  │   ├── local_translator.py     — บริการแปลภาษาผ่านโมเดลออฟไลน์ (M2M100) ต้องใช้ PyTorch
  │   ├── ocr.py                  — OCR สำรองด้วย EasyOCR (Legacy)
  │   ├── ocr_preprocessor.py     — เตรียมภาพสำหรับ OCR โดยใช้เทคนิค DBNet (ONNX) ค้นหาขอบเขตข้อความ และทำ Binarization
  │   ├── source_manager.py       — จัดการรายชื่อหน้าต่างและจอแสดงผลให้ผู้ใช้เลือก
  │   ├── translator.py           — คลาสกลางสำหรับจัดการแปลภาษาตัวดั้งเดิม
  │   ├── validation_service.py   — คัดกรองและลบข้อความ OCR ที่เป็นขยะหรือไม่ต้องการทิ้งไป
  │   └── windows_ocr.py          — เครื่องมือหลักในการทำ OCR ดึงพลังจาก Windows 10/11 SDK มาประมวลผลข้อความ
  │
  ├── ui/
  │   ├── main_window.py          — หน้าต่างควบคุมหลัก (Control Panel) และ Loop หลักของระบบแคปจอ (รองรับ Similarity Skip)
  │   ├── overlay.py              — หน้าต่างโปร่งใสที่แสดงผลคำแปลซ้อนทับเกมหรือแอปพลิเคชัน
  │   ├── theme.py                — จัดการสีสันและธีม (มืด/สว่าง) ของ UI
  │   └── components/
  │       ├── loading_spinner.py  — อนิเมชันโหลดระหว่างรอ
  │       ├── settings_dialog.py  — ป๊อปอัปตั้งค่า
  │       ├── settings_screen.py  — หน้าจอการตั้งค่าหลัก
  │       └── source_card.py      — การ์ดแสดงผลตัวเลือกหน้าจอ/หน้าต่าง
  │
  └── utils/
      ├── image_utils.py          — **[NEW]** ฟังก์ชันคำนวณความคล้ายของภาพ (`calculate_similarity`) ตัดการทำงานซ้ำซ้อน
      ├── single_instance.py      — ป้องกันไม่ให้ผู้ใช้เปิดโปรแกรมซ้อนกันหลายหน้าต่าง (ใช้ Named Pipe)
      └── singleton.py            — Singleton Metaclass ทำให้คลาสถูกสร้างเพียงครั้งเดียวและเรียกใช้ร่วมกันได้

================================================================================
สรุปการทำงานของระบบ (Pipeline ล่าสุด)
================================================================================
1. การดักจับภาพ (Capture)
   - ดึงภาพจากหน้าต่างเป้าหมายที่ความเร็วสูงสุดผ่าน `wgc_capture.py` (เคลียร์คิวภาพเก่าทิ้งเสมอ เพื่อให้ได้ภาพสดใหม่ที่สุด)
2. การข้ามการประมวลผล (Similarity Skip)
   - เปรียบเทียบความคล้ายคลึงของเฟรมปัจจุบันกับเฟรมที่แล้วใน `main_window.py` (ผ่าน `image_utils.py`) 
   - หากภาพคล้ายกันเกิน 85% จะข้ามการทำ OCR ไปเลยแล้วค้างคำแปลเดิมไว้บนหน้าจอ (ช่วยประหยัด CPU และ API อย่างมาก)
3. การเตรียมภาพ (Preprocessing)
   - ส่งภาพไปให้ `ocr_preprocessor.py` ใช้ DBNet AI หาขอบเขตข้อความ (Bounding Box) และทำภาพขาวดำเพื่อลด Noise
4. การสกัดข้อความ (OCR)
   - โยนภาพขาวดำเข้าไปใน `windows_ocr.py` เพื่ออ่านตัวอักษรออกมาทีละกล่อง
5. การแปลภาษา (Translation - Batching)
   - นำข้อความที่อ่านได้ "ทั้งหมดในเฟรมนั้น" มัดรวมกัน (Array)
   - ส่งออกไปยัง API แปลภาษา (เช่น `deepseek_translate.py` หรือ `gemini_translate.py`) ภายใน Request เดียว (Batch Processing)
   - รับคำแปลกลับมาเป็น JSON Array และแยกแมปกลับเข้ากับตำแหน่ง Bounding Box เดิม
6. การแสดงผล (Overlay)
   - ส่งข้อความที่แปลเสร็จแล้วไปวาดทับหน้าจอผ่าน `overlay.py` โดยที่ Overlay จะไม่ถูกซ่อนเมื่อผู้ใช้สลับหน้าต่าง (Persistent Visibility)
