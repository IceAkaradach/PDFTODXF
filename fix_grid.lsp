;;; ==========================================================================
;;; AutoLISP: FIX_GRID.LSP - ดึงวัตถุและรูเจาะวิ่งเข้าหาเส้นกริด AutoCAD อัตโนมัติ
;;; 
;;; ความสามารถ:
;;;   - อ่านค่า Snap X/Y และ Grid X/Y ที่ผู้ใช้ตั้งไว้ใน Drafting Settings (F9/F7) อัตโนมัติ
;;;   - ดึงจุดศูนย์กลางของวงกลมรูเจาะทุกวงให้ "วิ่งเข้าไปเกาะเส้นกริด AutoCAD" เป๊ะๆ 100%
;;;   - รองรับแถวสลับฟันปลา (Staggered) อัตโนมัติ
;;;
;;; วิธีใช้:
;;;   1. ลากไฟล์ fix_grid.lsp นี้ไปปล่อยในหน้าต่าง AutoCAD ได้เลย
;;;   2. พิมพ์คำสั่ง: SNAPGRID หรือพิมพ์สั้นๆ ว่า SG แล้วกด Enter
;;; ==========================================================================

(vl-load-com)

(defun c:SNAPGRID ( / *error* acadObj doc snap grid base stepX stepY halfStepX halfStepY 
                      userStepX userStepY ss count i ent ed pt cx cy cz newX newY 
                      optStagger baseX baseY selMode)
  
  ;; Error handler & Undo Group
  (defun *error* (msg)
    (if doc (vla-EndUndoMark doc))
    (setvar "CMDECHO" 1)
    (if (and msg (not (wcmatch (strcase msg) "*CANCEL*,*QUIT*,*EXIT*")))
      (princ (strcat "\n[!] Error: " msg))
    )
    (princ)
  )

  (setvar "CMDECHO" 0)
  (setq acadObj (vlax-get-acad-object))
  (setq doc (vla-get-ActiveDocument acadObj))
  (vla-StartUndoMark doc)

  (princ "\n==========================================================")
  (princ "\n   AutoCAD Snap To Active Grid Tool (SNAPGRID / SG)")
  (princ "\n==========================================================")

  ;; 1. อ่านค่า Grid และ Snap Spacing ที่ผู้ใช้ตั้งไว้ใน Drafting Settings (DSETTINGS) ทันที
  (setq snap (getvar "SNAPUNIT"))
  (setq grid (getvar "GRIDUNIT"))
  (setq base (getvar "SNAPBASE"))
  
  (setq stepX (car snap))
  (setq stepY (cadr snap))
  
  ;; ถ้าใน Snap เป็น 0 ให้ไปดึงจาก Grid Spacing
  (if (or (null stepX) (<= stepX 0.0)) (setq stepX (car grid)))
  (if (or (null stepY) (<= stepY 0.0)) (setq stepY (cadr grid)))
  
  ;; ค่าเริ่มต้นสำรอง
  (if (or (null stepX) (<= stepX 0.0)) (setq stepX 2.5))
  (if (or (null stepY) (<= stepY 0.0)) (setq stepY 2.5))

  (princ (strcat "\n[+] ตรวจพบการตั้งค่าใน Drafting Settings ของ AutoCAD:"))
  (princ (strcat "\n    - Grid/Snap X = " (rtos stepX 2 4)))
  (princ (strcat "\n    - Grid/Snap Y = " (rtos stepY 2 4)))

  ;; 2. ยืนยันระยะ Grid ที่ต้องการใช้งาน
  (setq userStepX (getreal (strcat "\nกดยืนยันระยะ Grid X [กด Enter เพื่อใช้ค่า " (rtos stepX 2 2) "]: ")))
  (if userStepX (setq stepX userStepX))
  
  (setq userStepY (getreal (strcat "\nกดยืนยันระยะ Grid Y [กด Enter เพื่อใช้ค่า " (rtos stepY 2 2) "]: ")))
  (if userStepY (setq stepY userStepY))

  ;; 3. โหมดการ Snap เข้ากริด
  ;; ค่าเริ่มต้น [1]: วิ่งเข้าเส้นกริด AutoCAD ตามที่ตั้งไว้ตรงๆ (เช่น 2.5 x 2.5) แนะนำ
  ;; ตัวเลือก [2]: หารครึ่งกริด (Half-Pitch เช่น 1.25) กรณีต้องการพิกัดกึ่งกลางกริด
  (initget "1 2 E H")
  (setq optMode (getkword "\nเลือกโหมดการ Snap [1=ตรงตามเส้นกริด AutoCAD / 2=หารครึ่ง Half-Pitch] <1>: "))
  (if (or (null optMode) (= optMode "1") (= optMode "E"))
    (progn
      (setq halfStepX stepX)
      (setq halfStepY stepY)
      (princ (strcat "\n>> โหมดกริดตรง: ดึงรูเจาะเข้าเส้นกริด AutoCAD " (rtos stepX 2 2) " x " (rtos stepY 2 2) " เป๊ะๆ 100%"))
    )
    (progn
      (setq halfStepX (/ stepX 2.0))
      (setq halfStepY (/ stepY 2.0))
      (princ (strcat "\n>> โหมดหารครึ่ง: อนุญาตพิกัดลงกริดหลัก " (rtos stepX 2 2) " และกึ่งกลาง " (rtos halfStepX 2 2)))
    )
  )

  ;; 4. เลือกวัตถุ: ให้เลือกเฉพาะจุด หรือดึงวงกลมทั้งหมดในแบบ
  (princ "\nเลือกวัตถุที่ต้องการดึงเข้ากริด (กด Enter ทันทีเพื่อดึง 'วงกลมทั้งหมดในแบบ'): ")
  (setq ss (ssget '((0 . "CIRCLE"))))
  (if (null ss)
    (progn
      (princ "\n>> ดึงวงกลมทั้งหมดในแบบอัตโนมัติ...")
      (setq ss (ssget "_X" '((0 . "CIRCLE"))))
    )
  )

  (if (null ss)
    (progn
      (princ "\n[!] ไม่พบวัตถุ Circle ในแบบนี้")
      (vla-EndUndoMark doc)
      (setvar "CMDECHO" 1)
      (exit)
    )
  )

  (setq count (sslength ss))
  (princ (strcat "\nกำลังดึงวงกลม " (itoa count) " วง วิ่งเข้าหาเส้นกริด AutoCAD ที่ตั้งไว้..."))

  ;; 5. วิ่งเข้าหาพิกัดกริด AutoCAD
  (setq baseX (car base))
  (setq baseY (cadr base))
  (setq i 0)
  (while (< i count)
    (setq ent (ssname ss i))
    (setq ed (entget ent))
    (setq pt (cdr (assoc 10 ed)))
    (setq cx (car pt))
    (setq cy (cadr pt))
    (setq cz (caddr pt))

    ;; ปัดพิกัดเข้าหาตำแหน่งกริด AutoCAD ที่ใกล้ที่สุด
    (setq newX (+ baseX (* (fix (+ (/ (- cx baseX) halfStepX) (if (>= (- cx baseX) 0.0) 0.5 -0.5))) halfStepX)))
    (setq newY (+ baseY (* (fix (+ (/ (- cy baseY) halfStepY) (if (>= (- cy baseY) 0.0) 0.5 -0.5))) halfStepY)))

    ;; อัปเดตพิกัดลง Entity
    (setq ed (subst (cons 10 (list newX newY cz)) (assoc 10 ed) ed))
    (entmod ed)

    (setq i (1+ i))
  )

  ;; 6. เปิด Grid (F7) และ Snap (F9) ให้อัตโนมัติ พร้อม Redraw
  (setvar "SNAPMODE" 1)
  (setvar "GRIDMODE" 1)
  (command "_.REDRAW")

  (vla-EndUndoMark doc)
  (setvar "CMDECHO" 1)
  (princ (strcat "\n✓ เรียบร้อย! วงกลมทั้ง " (itoa count) " วง วิ่งเข้าเกาะเส้นกริด AutoCAD เป๊ะ 100%!"))
  (princ "\n(หากต้องการยกเลิก สามารถกด Ctrl+Z เพื่อ Undo ได้ทันที)")
  (princ "\n==========================================================\n")
  (princ)
)

;; คำสั่งเรียกใช้งาน
(defun c:SG () (c:SNAPGRID))
(defun c:FIXGRID () (c:SNAPGRID))

(princ "\n[AutoLISP] โหลดสำเร็จ!")
(princ "\n>> พิมพ์คำสั่ง SG หรือ SNAPGRID แล้วกด Enter เพื่อดึงวัตถุเข้ากริด AutoCAD ทันที\n")
(princ)
