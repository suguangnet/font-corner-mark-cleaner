# -*- coding: utf-8 -*-
"""
字体占格角标批量清除工具
=========================
针对「田英章单字占格」一类字库：每个字的左上角、右下角都带有一个 L 形黑色占格角标
（竖+横的小笔画），本工具批量扫描所有字形，删除这两处角标轮廓，不留任何痕迹
（是删除轮廓，不是用白色覆盖）。

原理：
  通过全字库扫描确认，角标轮廓完全位于字身之外的两个极角区，真实笔画绝不会进入：
    · 左上角(TL)：整条轮廓 maxX 很小、minY 很高（在字身上方）
    · 右下角(BR)：整条轮廓 minX 很大、maxY 很低（在字身下方）
  按这两个几何判据删除对应轮廓即可，不会误伤任何文字笔画。

依赖：fonttools、Pillow（预览用，缺失时自动跳过预览）
"""

import os
import sys
import threading
import tempfile
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
from fontTools.ttLib.tables import ttProgram
import array


# ----------------------------------------------------------------------------
# 角标识别与删除核心逻辑
# ----------------------------------------------------------------------------
#
# 阈值用「相对 em 大小」表达，便于不同 unitsPerEm 的字体通用。
# 数值依据对本字库 6784 个字形的全量统计得出，并留有充足安全余量。
#
#   真实角标实测：
#     TL  bbox ≈ (-169,1674)-(71,1917)   点数 16~18
#     BR  bbox ≈ (2433,-916)-(2694,-667) 点数 16~18    （em = 2048）
#
TL_MAX_X = 0.20    # 左上角标：maxX < 0.20*em
TL_MIN_Y = 0.72    # 左上角标：minY > 0.72*em
BR_MIN_X = 1.05    # 右下角标：minX > 1.05*em
BR_MAX_Y = -0.25   # 右下角标：maxY < -0.25*em
MAX_PTS  = 40      # 角标点数很少，超过此数的轮廓一律视为真实笔画，绝不删除


def _contour_is_mark(pts, upm):
    """判断一条轮廓（点列表）是否为占格角标。"""
    if len(pts) > MAX_PTS:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mnx, mny, mxx, mxy = min(xs), min(ys), max(xs), max(ys)
    # 左上角
    if mxx < TL_MAX_X * upm and mny > TL_MIN_Y * upm:
        return True
    # 右下角
    if mnx > BR_MIN_X * upm and mxy < BR_MAX_Y * upm:
        return True
    return False


def clean_glyph(glyph, upm):
    """删除单个字形中的角标轮廓，返回删除的轮廓数量。"""
    # 复合字形(numberOfContours<0)或空字形不处理
    if glyph.numberOfContours <= 0:
        return 0

    ends = glyph.endPtsOfContours
    coords = glyph.coordinates
    flags = glyph.flags

    new_coords = []
    new_flags = []
    new_ends = []
    removed = 0
    start = 0

    for end in ends:
        seg = range(start, end + 1)
        pts = [coords[k] for k in seg]
        if _contour_is_mark(pts, upm):
            removed += 1
        else:
            for k in seg:
                new_coords.append(coords[k])
                new_flags.append(flags[k])
            new_ends.append(len(new_coords) - 1)
        start = end + 1

    if removed:
        glyph.coordinates = GlyphCoordinates(new_coords)
        glyph.flags = array.array("B", new_flags)
        glyph.endPtsOfContours = new_ends
        glyph.numberOfContours = len(new_ends)
        # 删点后旧的 TrueType 指令（hinting）会引用到已不存在的点，直接清空避免渲染异常
        if hasattr(glyph, "program"):
            prog = ttProgram.Program()
            prog.fromBytecode(b"")
            glyph.program = prog

    return removed


def process_font(in_path, out_path, progress_cb=None, log_cb=None):
    """处理整套字体，返回统计字典。"""
    font = TTFont(in_path)
    if "glyf" not in font:
        raise ValueError("该字体不是 TrueType(glyf) 轮廓字体，本工具暂不支持。")

    glyf = font["glyf"]
    upm = font["head"].unitsPerEm
    order = font.getGlyphOrder()
    total = len(order)

    glyphs_changed = 0
    contours_removed = 0

    for i, gname in enumerate(order):
        glyph = glyf[gname]
        r = clean_glyph(glyph, upm)
        if r:
            glyphs_changed += 1
            contours_removed += r
            # 重算该字形包围盒
            glyph.recalcBounds(glyf)
            # 关键：同步更新水平度量。
            # 删除左上角角标后，字形真实左边界(xMin)变大了，但 hmtx 里的左边距(lsb)
            # 仍是角标存在时的旧值。GDI/DirectWrite 渲染时会按 lsb 平移字形，
            # 导致字的位置偏移。保持字宽(advanceWidth)不变、把 lsb 对齐到新的 xMin，
            # 即可让字位置保持不动（与在字体软件里手动删除的效果一致）。
            aw, _ = font["hmtx"][gname]
            new_lsb = glyph.xMin if glyph.numberOfContours > 0 and hasattr(glyph, "xMin") else 0
            font["hmtx"][gname] = (aw, int(new_lsb))
            if log_cb and glyphs_changed <= 8:
                log_cb(f"  · {gname}: 删除 {r} 个角标轮廓")
        if progress_cb and (i % 200 == 0 or i == total - 1):
            progress_cb(i + 1, total)

    # 重算整套字体的全局包围盒（head 表），去掉被角标撑大的范围
    _recalc_font_bbox(font, glyf)

    font.save(out_path)
    font.close()

    return {
        "total_glyphs": total,
        "glyphs_changed": glyphs_changed,
        "contours_removed": contours_removed,
        "out_path": out_path,
    }


def _recalc_font_bbox(font, glyf):
    head = font["head"]
    xmins = ymins = xmaxs = ymaxs = None
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        if getattr(g, "numberOfContours", 0) == 0:
            continue
        try:
            g.recalcBounds(glyf)
        except Exception:
            continue
        if not hasattr(g, "xMin"):
            continue
        if xmins is None:
            xmins, ymins, xmaxs, ymaxs = g.xMin, g.yMin, g.xMax, g.yMax
        else:
            xmins = min(xmins, g.xMin); ymins = min(ymins, g.yMin)
            xmaxs = max(xmaxs, g.xMax); ymaxs = max(ymaxs, g.yMax)
    if xmins is not None:
        head.xMin, head.yMin, head.xMax, head.yMax = xmins, ymins, xmaxs, ymaxs


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("字体占格角标批量清除工具")
        root.minsize(760, 560)
        self._center_window(820, 680)

        self.in_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.sample_char = tk.StringVar(value="拙")

        pad = dict(padx=10, pady=6)

        # —— 文件选择区 ——
        frm = ttk.LabelFrame(root, text="字体文件")
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="源字体：").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.in_path).grid(row=0, column=1, sticky="we", padx=6, pady=6)
        ttk.Button(frm, text="浏览…", command=self.choose_in).grid(row=0, column=2, padx=6, pady=6)

        ttk.Label(frm, text="输出到：").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.out_path).grid(row=1, column=1, sticky="we", padx=6, pady=6)
        ttk.Button(frm, text="另存为…", command=self.choose_out).grid(row=1, column=2, padx=6, pady=6)
        frm.columnconfigure(1, weight=1)

        # —— 操作区 ——
        opt = ttk.Frame(root)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="预览字符：").pack(side="left")
        ttk.Entry(opt, textvariable=self.sample_char, width=4).pack(side="left")
        ttk.Button(opt, text="预览处理前", command=self.preview_before).pack(side="left", padx=6)
        self.btn_run = ttk.Button(opt, text="开始批量清除角标", command=self.run)
        self.btn_run.pack(side="right")

        # —— 预览区 ——
        prev = ttk.LabelFrame(root, text="预览（左：处理前  右：处理后）")
        prev.pack(fill="x", **pad)
        self.canvas_before = tk.Canvas(prev, width=260, height=260, bg="white", highlightthickness=1, highlightbackground="#ccc")
        self.canvas_before.pack(side="left", padx=12, pady=10)
        self.canvas_after = tk.Canvas(prev, width=260, height=260, bg="white", highlightthickness=1, highlightbackground="#ccc")
        self.canvas_after.pack(side="left", padx=12, pady=10)
        self._imgs = {}  # 防止 PhotoImage 被回收

        # —— 进度 + 日志 ——
        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", **pad)

        # —— 底部版权（先于可伸缩的日志框打包，固定在窗口底部）——
        footer = tk.Frame(root, bg="#f0f0f0")
        footer.pack(side="bottom", fill="x")
        tk.Label(
            footer,
            text="@2026 速光网络软件开发   suguang.cc   抖音：dubaishun12",
            bg="#f0f0f0", fg="#666", font=("Microsoft YaHei", 9),
        ).pack(pady=5)

        logf = ttk.LabelFrame(root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=8, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.log.config(yscrollcommand=sb.set)

        self._log("就绪。请选择源字体后点击“开始批量清除角标”。")
        self._log("说明：本字库实测仅有【左上角】和【右下角】两处角标，会被全部删除。")

    # ---------------- 工具方法 ----------------
    def _center_window(self, w, h):
        """让窗口在屏幕居中打开。"""
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = int((sw - w) / 2)
        y = int((sh - h) / 2 - 20)
        if y < 0:
            y = 0
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")
        self.root.update_idletasks()

    def choose_in(self):
        p = filedialog.askopenfilename(
            title="选择源字体",
            filetypes=[("字体文件", "*.otf *.ttf"), ("所有文件", "*.*")],
        )
        if p:
            self.in_path.set(p)
            base, ext = os.path.splitext(p)
            self.out_path.set(base + "_已清角标" + ext)

    def choose_out(self):
        ext = os.path.splitext(self.in_path.get())[1] or ".ttf"
        p = filedialog.asksaveasfilename(
            title="输出字体另存为",
            defaultextension=ext,
            filetypes=[("字体文件", "*.otf *.ttf"), ("所有文件", "*.*")],
        )
        if p:
            self.out_path.set(p)

    # ---------------- 预览 ----------------
    def _render(self, canvas, font_path, char):
        canvas.delete("all")
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
        except Exception:
            canvas.create_text(130, 130, text="（未安装 Pillow，\n无法预览）", justify="center", fill="#999")
            return
        try:
            size = 200
            W = H = 260
            img = Image.new("RGB", (W, H), "white")
            d = ImageDraw.Draw(img)
            fnt = ImageFont.truetype(font_path, size)
            # 居中绘制
            try:
                bbox = d.textbbox((0, 0), char, font=fnt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                x = (W - tw) / 2 - bbox[0]
                y = (H - th) / 2 - bbox[1]
            except Exception:
                x = y = 30
            d.text((x, y), char, font=fnt, fill="black")
            ph = ImageTk.PhotoImage(img)
            self._imgs[id(canvas)] = ph  # 保活
            canvas.create_image(0, 0, anchor="nw", image=ph)
        except Exception as e:
            canvas.create_text(130, 130, text="预览失败：\n" + str(e), justify="center", fill="#c00")

    def preview_before(self):
        p = self.in_path.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showwarning("提示", "请先选择有效的源字体文件。")
            return
        ch = (self.sample_char.get() or "拙")[0]
        self._render(self.canvas_before, p, ch)

    # ---------------- 处理 ----------------
    def run(self):
        ip = self.in_path.get().strip()
        op = self.out_path.get().strip()
        if not ip or not os.path.isfile(ip):
            messagebox.showwarning("提示", "请先选择有效的源字体文件。")
            return
        if not op:
            base, ext = os.path.splitext(ip)
            op = base + "_已清角标" + ext
            self.out_path.set(op)
        if os.path.abspath(ip) == os.path.abspath(op):
            messagebox.showwarning("提示", "输出路径不能与源字体相同，请另选输出文件。")
            return

        self.btn_run.config(state="disabled")
        self.progress.config(value=0, maximum=100)
        self._log("\n开始处理：" + os.path.basename(ip))
        # 处理前先渲染一张
        ch = (self.sample_char.get() or "拙")[0]
        self._render(self.canvas_before, ip, ch)

        t = threading.Thread(target=self._worker, args=(ip, op, ch), daemon=True)
        t.start()

    def _worker(self, ip, op, ch):
        def progress_cb(done, total):
            self.root.after(0, lambda: self._set_progress(done, total))

        def log_cb(msg):
            self.root.after(0, lambda: self._log(msg))

        try:
            stats = process_font(ip, op, progress_cb=progress_cb, log_cb=log_cb)
            self.root.after(0, lambda: self._done(stats, ch))
        except Exception as e:
            err = traceback.format_exc()
            self.root.after(0, lambda: self._fail(e, err))

    def _set_progress(self, done, total):
        self.progress.config(maximum=total, value=done)

    def _done(self, stats, ch):
        self._log("─" * 40)
        self._log(f"完成！共 {stats['total_glyphs']} 个字形，"
                  f"其中 {stats['glyphs_changed']} 个字删除了角标，"
                  f"累计删除 {stats['contours_removed']} 条角标轮廓。")
        self._log("已保存到：" + stats["out_path"])
        # 渲染处理后效果
        self._render(self.canvas_after, stats["out_path"], ch)
        self.btn_run.config(state="normal")
        messagebox.showinfo("完成",
                            f"处理完成！\n\n删除角标的字数：{stats['glyphs_changed']}\n"
                            f"删除轮廓总数：{stats['contours_removed']}\n\n"
                            f"输出文件：\n{stats['out_path']}")

    def _fail(self, e, err):
        self._log("【出错】" + str(e))
        self._log(err)
        self.btn_run.config(state="normal")
        messagebox.showerror("出错", str(e))


def main():
    root = tk.Tk()
    # 高 DPI 友好
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
