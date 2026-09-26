;;; ==========================================================================
;;; AutoLISP: FIX_GRID.LSP - จัดพิกัด Origin (0,0) และล็อกระยะกริดรูเจาะใน AutoCAD
;;; วิธีใช้:
;;;   1. ลากไฟล์ fix_grid.lsp นี้ไปปล่อยในหน้าต่าง AutoCAD ได้เลย
;;;   2. พิมพ์คำสั่ง FIXGRID แล้วกด Enter
;;; ==========================================================================

(vl-load-com)

(defun c:FIXGRID ( / ssAll ssCirc minPt maxPt bmin bmax ent ed pt cx cy cz
                     count i step testPt autoStep userStep newX newY optMove)
  (setvar "CMDECHO" 0)
  (princ "\n==========================================================")
  (princ "\n   AutoCAD Grid & Pitch Alignment Tool (FIXGRID)")
  (princ "\n==========================================================")

  ;; 1. ตรวจสอบวงกลมทั้งหมดในงาน
  (setq ssCirc (ssget "_X" '((0 . "CIRCLE"))))
  (if (null ssCirc)
    (progn
      (princ "\n[!] ไม่พบวัตถุ Circle ในแบบนี้")
      (setvar "CMDECHO" 1)
      (exit)
    )
  )
  (setq count (sslength ssCirc))

  ;; 2. Auto-detect หน่วย/สเกลของแบบ (ดูจากพิกัดตัวอย่าง)
  (setq testPt (cdr (assoc 10 (entget (ssname ssCirc 0)))))
  ;; ถ้าพิกัดหลักสิบ/หลักร้อยต้นๆ -> สเกล 1:1 mm (ระยะ 1.25 / 2.5)
  ;; ถ้าพิกัดหลักพัน/หลักหมื่น -> สเกล 100x (ระยะ 125 / 250)
  (if (> (car testPt) 500.0)
    (setq autoStep 125.0)
    (setq autoStep 1.25)
  )

  ;; 3. ให้ผู้ใช้เลือกหรือยืนยันระยะ Grid Step
  (princ (strcat "\nตรวจพบสเกลงาน แนะนำ Grid Step: " (rtos autoStep 2 2)))
  (setq userStep (getreal (strcat "\nระบุ Grid Step ที่ต้องการ [กด Enter เพื่อใช้ " (rtos autoStep 2 2) "]: ")))
  (if (null userStep) (setq step autoStep) (setq step userStep))

  ;; 4. ถามเรื่องการย้าย Origin ไปที่ (0, 0)
  (initget "Y N")
  (setq optMove (getkword "\nต้องการย้ายมุมล่างซ้ายของงานไปที่จุด (0, 0) ด้วยหรือไม่? [Yes/No] <Y>: "))
  (if (or (null optMove) (= optMove "Y"))
    (progn
      (setq ssAll (ssget "_X"))
      (setq minPt '(1e99 1e99 0.0))
      (setq i 0)
      (while (< i (sslength ssAll))
        (setq ent (ssname ssAll i))
        (if (vlax-write-enabled-p (vlax-ename->vla-object ent))
          (progn
            (vla-getboundingbox (vlax-ename->vla-object ent) 'bmin 'bmax)
            (setq bmin (vlax-safearray->list bmin))
            (setq minPt (list (min (car minPt) (car bmin))
                              (min (cadr minPt) (cadr bmin))
                              0.0))
          )
        )
        (setq i (1+ i))
      )
      ;; ย้ายทุกวัตถุไปที่พิกัด (0, 0)
      (command "_.MOVE" ssAll "" minPt '(0.0 0.0 0.0))
      (princ "\n✓ ย้ายมุมล่างซ้ายไปที่ (0, 0, 0) เรียบร้อย")
    )
  )

  ;; 5. จัดพิกัดศูนย์กลางวงกลมทุกวงให้ลงตัวตาม Grid Step (กำจัดเศษทศนิยม .03, .15)
  ;; หาค่า Offset ฐาน เพื่อไม่ให้ตำแหน่งโดยรวมเคลื่อน
  (setq i 0 sumModX 0.0 sumModY 0.0 sampleCount (min 100 count))
  (while (< i sampleCount)
    (setq pt (cdr (assoc 10 (entget (ssname ssCirc i)))))
    (setq sumModX (+ sumModX (rem (car pt) step)))
    (setq sumModY (+ sumModY (rem (cadr pt) step)))
    (setq i (1+ i))
  )
  (setq baseOffsetX (/ sumModX sampleCount))
  (setq baseOffsetY (/ sumModY sampleCount))

  (princ (strcat "\nกำลังประมวลผลวงกลม " (itoa count) " วง..."))
  (setq i 0)
  (while (< i count)
    (setq ent (ssname ssCirc i))
    (setq ed (entget ent))
    (setq pt (cdr (assoc 10 ed)))
    (setq cx (car pt))
    (setq cy (cadr pt))
    (setq cz (caddr pt))

    ;; คำนวณตำแหน่ง Grid ที่สมบูรณ์แบบ
    (setq newX (+ baseOffsetX (* (fix (+ (/ (- cx baseOffsetX) step) 0.5)) step)))
    (setq newY (+ baseOffsetY (* (fix (+ (/ (- cy baseOffsetY) step) 0.5)) step)))

    ;; ปรับค่าพิกัดใน AutoCAD Database ทันที
    (setq ed (subst (cons 10 (list newX newY cz)) (assoc 10 ed) ed))
    (entmod ed)

    (setq i (1+ i))
  )
  (princ (strcat "\n✓ ล็อกพิกัดวงกลมครบทั้ง " (itoa count) " วงเข้ากริด " (rtos step 2 2) " เรียบร้อย!"))

  ;; 6. ปรับการตั้งค่า AutoCAD ให้รองรับ Grid & Snap
  (setvar "GRIDUNIT" (list step step))
  (setvar "SNAPUNIT" (list step step))
  (setvar "GRIDMODE" 1)
  (command "_.ZOOM" "_E")

  (setvar "CMDECHO" 1)
  (princ "\n==========================================================")
  (princ "\n✓ เสร็จสิ้น! ระยะห่างระหว่างรู = เป๊ะตามกริด ไม่มีเศษทศนิยม")
  (princ "\n==========================================================\n")
  (princ)
)

;; คำสั่งลัด
(defun c:SNAP250 () (c:FIXGRID))
(defun c:SG () (c:FIXGRID))

(princ "\n[PDF to DXF] โหลดสคริปต์ AutoLISP เรียบร้อย!")
(princ "\n>> พิมพ์คำสั่ง FIXGRID หรือ SG แล้วกด Enter เพื่อจัดกริดและพิกัด (0,0)\n")
(princ)
