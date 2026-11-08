# -*- coding: utf-8 -*-
# 批量打包 C4D 工程 — 将文件夹内 .c4d 工程批量打包为独立文件夹（含资源）
# 功能：路径选择 / 刷新列表 / 勾选打包 / 全选全不选 / 滚动列表 / 自然排序 / 实时计数 / 空壳检测
#       缺失资源报告（按工程分组）/ 失败半成品文件夹自动标记「-打包失败」

import os
import re
import sys
import codecs
import traceback
import c4d
from c4d import documents, gui, storage


def _to_unicode(s):
    """路径转 Unicode，供 os.path / os.listdir 使用。"""
    if sys.version_info[0] >= 3:
        return s
    if isinstance(s, unicode):  # noqa: F821
        return s
    return s.decode("utf-8")


def _to_utf8(s):
    """路径转 UTF-8 字节串，供 C4D API (LoadDocument/SaveProject) 使用。
    R20 的 Maxon URL 接口不接受 Unicode，也不接受 GBK，只认 UTF-8。"""
    if sys.version_info[0] >= 3:
        return s
    if isinstance(s, unicode):  # noqa: F821
        return s.encode("utf-8")
    return s


def _log(s):
    """安全打印，Python 2 下 Unicode 显式编码为 UTF-8。"""
    if sys.version_info[0] < 3:
        if isinstance(s, unicode):  # noqa: F821
            s = s.encode("utf-8")
    print(s)


def _natural_sort_key(s):
    """自然排序：1, 2, ... 9, 10, 11（非字符串排序）。"""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', s)]


# ==================== 自绘列表（整行点击 / 高亮细描边） ====================
ROW_H = 24
_DT_LEFT = getattr(c4d, "DRAWTEXT_LEFT", 0)  # 老版本缺常量时退化为默认对齐


def _rgb_of(c):
    """GetColorRGB 返回 dict{'r','g','b'}，自定义色是 tuple，统一转 (r, g, b)。"""
    if isinstance(c, dict):
        return (c["r"], c["g"], c["b"])
    return (int(c[0]), int(c[1]), int(c[2]))


def _blend(base, tint, t):
    """两个颜色按比例混合，返回 (r, g, b)。"""
    b = _rgb_of(base)
    c = _rgb_of(tint)
    return tuple(int(round(b[i] * (1.0 - t) + c[i] * t)) for i in range(3))


class FileListArea(gui.GeUserArea):
    """工程文件列表：整行可点击勾选；Shift+点击区间连选。"""

    def __init__(self, owner):
        super(FileListArea, self).__init__()
        self._owner = owner
        self._draw_err_logged = False  # 绘制异常只打印一次，避免刷屏

    def reset(self):
        self.redraw()

    def Init(self):
        # 离屏缓冲：低版本重绘时避免整个列表闪烁
        try:
            self.OffScreenOn()
        except Exception:
            pass
        return True

    def redraw(self):
        try:
            self.Redraw()
            return
        except AttributeError:
            pass
        try:
            self.LayoutChanged()
        except Exception:
            self._owner.LayoutChanged(self._owner.ID_LIST_AREA)

    def _pen(self, rgb):
        """设置画笔。不同版本 DrawSetPen 参数形式不同，逐个尝试。"""
        r, g, b = _rgb_of(rgb)
        try:
            self.DrawSetPen(r, g, b)
        except TypeError:
            try:
                self.DrawSetPen((r, g, b))
            except TypeError:
                self.DrawSetPen(c4d.Vector(r / 255.0, g / 255.0, b / 255.0))

    def _round_fill(self, X1, Y1, X2, Y2, r):
        """圆角填充：中间两块矩形 + 四角椭圆，失败回退普通矩形。r 可为浮点。"""
        try:
            ri = int(r)
            self.DrawRectangle(X1 + ri, Y1, X2 - ri, Y2)
            self.DrawRectangle(X1, Y1 + ri, X2, Y2 - ri)
            self.DrawEllipseFill([X1 + r, Y1 + r], [r, r])
            self.DrawEllipseFill([X2 - r, Y1 + r], [r, r])
            self.DrawEllipseFill([X1 + r, Y2 - r], [r, r])
            self.DrawEllipseFill([X2 - r, Y2 - r], [r, r])
        except Exception:
            self.DrawRectangle(X1, Y1, X2, Y2)

    def _text_col(self, fg_rgb, bg_rgb):
        """设置文字颜色（DrawSetTextCol 收 0-1 Vector）。"""
        try:
            f = _rgb_of(fg_rgb)
            b = _rgb_of(bg_rgb)
            self.DrawSetTextCol(c4d.Vector(f[0] / 255.0, f[1] / 255.0, f[2] / 255.0),
                                c4d.Vector(b[0] / 255.0, b[1] / 255.0, b[2] / 255.0))
        except Exception:
            pass

    def _local_xy(self, fx, fy):
        """鼠标坐标 -> 列表内容坐标。优先用官方 Global2Local（能正确处理滚动偏移），
        老版本没有该接口时退化为手动减去控件位置。"""
        try:
            p = self.Global2Local(fx, fy)
            return int(p["x"]), int(p["y"])
        except Exception:
            pass
        try:
            d = self._owner.GetItemDim(self._owner.ID_LIST_AREA)
            if d:
                return fx - int(d["x"]), fy - int(d["y"])
        except Exception:
            pass
        return fx, fy

    def GetMinSize(self, *args):
        width = args[0] if args else 0
        n = len(self._owner._file_paths)
        return (max(width, 260), ROW_H * max(1, n))

    # ---------- 绘制 ----------
    # 注意：不同 C4D 版本回调实参个数不同（新版 DrawMsg 多一个 msg 参、
    # Message 多一个 result 参、GetMinSize 无参），统一用 *args 兼容。
    def DrawMsg(self, x1, y1, x2, y2, *args):
        try:
            self._draw(x1, y1, x2, y2)
        except Exception:
            if not self._draw_err_logged:
                self._draw_err_logged = True
                _log(traceback.format_exc())

    def _draw(self, x1, y1, x2, y2):
        if hasattr(self, "SetClippingRegion"):
            self.SetClippingRegion(int(x1), int(y1),
                                   max(0, int(x2) - int(x1) + 1),
                                   max(0, int(y2) - int(y1) + 1))
        owner = self._owner
        files = owner._file_paths
        n = len(files)

        self.DrawSetPen(c4d.COLOR_BG)
        self.DrawRectangle(x1, y1, x2, y2)

        if n == 0:
            if owner.list_loaded:
                self.DrawText("（该文件夹中没有 .c4d 文件）", x1 + 8, y1 + 5, _DT_LEFT)
            return

        bg = self.GetColorRGB(c4d.COLOR_BG)
        txt = self.GetColorRGB(c4d.COLOR_TEXT)
        gray = _blend(bg, (170, 170, 170), 0.06)      # 选中行底：比背景稍亮
        box_edge = _blend(bg, txt, 0.55)              # 未勾选框边框
        check_fill = (115, 124, 230)                  # 勾选框：#737ce6
        check_mark = (255, 255, 255)                  # 白色对勾
        yellow = (255, 180, 50)                       # 选中文件名：#ffcc33

        row_start = max(0, int(y1) // ROW_H)
        row_end = min(n, int(y2) // ROW_H + 1)
        for i in range(row_start, row_end):
            ry = i * ROW_H
            checked = owner._checked[i]

            # 选中行：浅灰圆角高亮
            if checked:
                self._pen(gray)
                self._round_fill(x1 + 2, ry + 1, x2 - 3, ry + ROW_H - 2, 5)

            # 勾选框指示（12px；C4D 坐标含端点，右界 +11）
            bx = x1 + 10
            by = ry + (ROW_H - 12) // 2
            if checked:
                self._pen(check_fill)
                self._round_fill(bx, by, bx + 11, by + 11, 1.5)
                self._pen(check_mark)
                # 双层对勾，总包围 8 宽 6 高
                self.DrawLine(bx + 2, by + 5, bx + 4, by + 7)
                self.DrawLine(bx + 4, by + 7, bx + 9, by + 3)
                self.DrawLine(bx + 2, by + 6, bx + 4, by + 8)
                self.DrawLine(bx + 4, by + 8, bx + 9, by + 4)
            else:
                # 粗环：外圈 1.5 圆角，内圈直角掏空，壁厚 2
                self._pen(box_edge)
                self._round_fill(bx, by, bx + 11, by + 11, 1.5)
                self._pen(bg)
                self.DrawRectangle(bx + 2, by + 2, bx + 9, by + 9)

            # 文件名：选中变黄
            if checked:
                self._text_col(yellow, gray)
            else:
                self._text_col(txt, bg)
            self.DrawText(os.path.basename(files[i]), bx + 20, ry + 5, _DT_LEFT)

    # ---------- 输入 ----------
    def Message(self, msg, *args):
        if msg.GetId() == c4d.BFM_INPUT:
            if msg.GetInt32(c4d.BFM_INPUT_CHANNEL) == c4d.BFM_INPUT_MOUSELEFT:
                if msg.GetInt32(c4d.BFM_INPUT_DOUBLECLICK):
                    # 双击的第二下带 DOUBLECLICK 标记，忽略，否则勾选会翻两次等于没点
                    return True
                x = msg.GetInt32(c4d.BFM_INPUT_X)
                y = msg.GetInt32(c4d.BFM_INPUT_Y)
                q = msg.GetInt32(c4d.BFM_INPUT_QUALIFIER)
                self._on_press(x, y, bool(q & c4d.QSHIFT))
                return True
        try:
            return c4d.gui.GeUserArea.Message(self, msg, *args)
        except Exception:
            return True

    def _on_press(self, x, y, shift):
        x, y = self._local_xy(x, y)
        owner = self._owner
        idx = y // ROW_H
        if 0 <= idx < len(owner._file_paths):
            owner._handle_row_click(idx, shift)
            self.redraw()


class BatchPackProjectsDialog(gui.GeDialog):
    ID_BROWSE      = 1001
    ID_FOLDER_EDIT = 1002
    ID_REFRESH     = 1003
    ID_SELECT_ALL  = 1004
    ID_SELECT_NONE = 1005
    ID_SCROLL      = 1006
    ID_LIST_INNER  = 1007
    ID_LIST_AREA   = 1011
    ID_COUNT       = 1010
    ID_OK_BTN      = 1008
    ID_CANCEL_BTN  = 1009

    def __init__(self):
        super(BatchPackProjectsDialog, self).__init__()
        self.folder_path = ""
        self.list_loaded = False
        self._file_paths = []
        self._checked = []          # 自己记录每项勾选状态
        self._last_click_idx = -1   # Shift 连选的锚点行
        self._area = FileListArea(self)

    # ==================== 布局 ====================
    def CreateLayout(self):
        self.SetTitle("批量打包 C4D 工程")

        # 路径输入
        self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=3)
        self.AddStaticText(0, c4d.BFH_LEFT, name="文件夹:")
        self.AddEditText(self.ID_FOLDER_EDIT, c4d.BFH_SCALEFIT)
        self.AddButton(self.ID_BROWSE, c4d.BFH_LEFT, name="浏览...")
        self.GroupEnd()

        # 提示
        self.AddStaticText(0, c4d.BFH_LEFT,
                           name="不刷新直接确定 = 打包整个文件夹；刷新后可勾选")

        # 操作按钮行
        self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=3)
        self.AddButton(self.ID_REFRESH, c4d.BFH_LEFT, name="刷新列表")
        self.AddButton(self.ID_SELECT_ALL, c4d.BFH_LEFT, name="全选")
        self.AddButton(self.ID_SELECT_NONE, c4d.BFH_LEFT, name="全不选")
        self.GroupEnd()

        # 已选计数
        self.AddStaticText(self.ID_COUNT, c4d.BFH_LEFT, name="（未加载列表）")

        # 提示文字
        self.AddStaticText(0, c4d.BFH_LEFT,
                           name="（点击「刷新列表」加载工程文件）")
        self.AddStaticText(0, c4d.BFH_LEFT,
                           name="（若存在同名文件夹会自动添加数字后缀「例：工程-1」）")
        self.AddStaticText(0, c4d.BFH_LEFT,
                           name="（点击列表行勾选/取消；Shift+点击区间连选）")

        # 滚动列表区
        scroll_flags = c4d.SCROLLGROUP_VERT
        if hasattr(c4d, "SCROLLGROUP_AUTO"):
            scroll_flags |= c4d.SCROLLGROUP_AUTO
        self.ScrollGroupBegin(self.ID_SCROLL, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, scroll_flags)
        self.GroupBegin(self.ID_LIST_INNER, c4d.BFH_SCALEFIT | c4d.BFV_FIT, cols=1)
        self.AddUserArea(self.ID_LIST_AREA, c4d.BFH_SCALEFIT | c4d.BFV_TOP)
        self.AttachUserArea(self._area, self.ID_LIST_AREA)
        self.GroupEnd()
        self.GroupEnd()

        # 底部按钮
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFH_RIGHT, cols=2)
        self.AddButton(self.ID_OK_BTN, c4d.BFH_LEFT, name="确定")
        self.AddButton(self.ID_CANCEL_BTN, c4d.BFH_LEFT, name="取消")
        self.GroupEnd()

        return True

    def InitValues(self):
        self.SetString(self.ID_FOLDER_EDIT, "")
        return True

    # ==================== 辅助方法 ====================
    def _update_count(self):
        total = len(self._checked)
        if total == 0:
            if self.list_loaded:
                self.SetString(self.ID_COUNT, "已选 0 / 共 0")
            else:
                self.SetString(self.ID_COUNT, "（未加载列表）")
        else:
            sel = sum(1 for c in self._checked if c)
            self.SetString(self.ID_COUNT, "已选 {} / 共 {}".format(sel, total))

    def _populate_list(self):
        folder = _to_unicode(self.GetString(self.ID_FOLDER_EDIT).strip())
        if not folder or not os.path.isdir(folder):
            gui.MessageDialog("文件夹路径无效，请检查。")
            return
        self.folder_path = folder
        files = get_c4d_files(folder)

        self._file_paths = files
        self._checked = [True] * len(files)
        self._last_click_idx = -1
        self._area.reset()

        self.list_loaded = True
        self.LayoutChanged(self.ID_LIST_INNER)
        self.LayoutChanged(self.ID_SCROLL)
        self._update_count()

    def _handle_row_click(self, idx, shift_held):
        """处理列表行点击：单击切换；Shift+点击为区间连选。"""
        if idx < 0 or idx >= len(self._checked):
            return

        if shift_held and self._last_click_idx >= 0:
            target = not self._checked[idx]
            start = min(self._last_click_idx, idx)
            end = max(self._last_click_idx, idx)
            for i in range(start, end + 1):
                self._checked[i] = target
        else:
            self._checked[idx] = not self._checked[idx]

        self._last_click_idx = idx
        self._update_count()

    # ==================== 事件处理 ====================
    def Command(self, dialog_id, msg):
        if dialog_id == self.ID_BROWSE:
            folder = storage.LoadDialog(title="选择工程文件夹", flags=c4d.FILESELECT_DIRECTORY)
            if folder:
                self.SetString(self.ID_FOLDER_EDIT, folder)
            return True

        if dialog_id == self.ID_REFRESH:
            self._populate_list()
            return True

        if dialog_id == self.ID_SELECT_ALL:
            for i in range(len(self._checked)):
                self._checked[i] = True
            self._area.redraw()
            self._update_count()
            return True

        if dialog_id == self.ID_SELECT_NONE:
            for i in range(len(self._checked)):
                self._checked[i] = False
            self._area.redraw()
            self._update_count()
            return True

        if dialog_id == self.ID_OK_BTN:
            folder = _to_unicode(self.GetString(self.ID_FOLDER_EDIT).strip())
            if not folder:
                gui.MessageDialog("请输入或选择一个文件夹路径。")
                return True
            if not os.path.isdir(folder):
                gui.MessageDialog("路径不存在:\n{}".format(folder))
                return True

            same_folder = (os.path.normcase(os.path.normpath(folder)) ==
                           os.path.normcase(os.path.normpath(self.folder_path)))
            if self.list_loaded and self._file_paths and same_folder:
                selected = [self._file_paths[i] for i, c in enumerate(self._checked) if c]
                if not selected:
                    gui.MessageDialog("请至少勾选一个工程文件。")
                    return True
                files = selected
            else:
                files = get_c4d_files(folder)
                if not files:
                    gui.MessageDialog("所选文件夹中没有 .c4d 文件。")
                    return True

            self._pending_files = files
            self.SetTimer(80)
            return True

        if dialog_id == self.ID_CANCEL_BTN:
            self.Close()
            return True

        return True

    def AskClose(self):
        return False

    def Timer(self, msg):
        """OK 按下后延迟触发：关旧对话框 -> 开进度对话框，分两帧完成。"""
        self.SetTimer(0)
        files = getattr(self, "_pending_files", None)
        if files:
            self._pending_files = None
            self.Close()
            _start_packing(files)


# ==================== 进度对话框 ====================
class ProgressDialog(gui.GeDialog):
    ID_STOP_BTN       = 3001
    ID_PROGRESS_TEXT  = 3002

    PHASE_LOAD = 0
    PHASE_SAVE = 1
    PHASE_STAT = 2

    def __init__(self, file_list):
        super(ProgressDialog, self).__init__()
        self._file_list = file_list
        self._index = 0
        self._total = len(file_list)
        self._success = 0
        self._missing_by_project = {}   # 工程名 -> [缺失资源...]（按工程分组，方便对号入座）
        self._stop_requested = False
        self._finished = False
        self._phase = self.PHASE_LOAD
        self._job = None

    def CreateLayout(self):
        self.SetTitle("打包进行中")
        self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=1)
        self.GroupBorderSpace(12, 8, 12, 8)
        self.AddStaticText(self.ID_PROGRESS_TEXT, c4d.BFH_SCALEFIT,
                           initw=250, inith=20, name="正在准备...")
        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)
        self.GroupBegin(0, c4d.BFH_RIGHT, cols=1)
        self.AddButton(self.ID_STOP_BTN, c4d.BFH_RIGHT,
                       initw=150, inith=24, name="本工程完成后停止")
        self.GroupEnd()
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetTimer(50)
        self._show_phase("加载")
        return True

    def _show_phase(self, label, index=None):
        if index is None:
            index = self._index
        name = os.path.basename(self._file_list[index])
        pct = int(float(index) / float(self._total) * 100.0)
        self.SetString(self.ID_PROGRESS_TEXT,
                       "{} ({}/{}  {}%): {}".format(
                           label, index + 1, self._total, pct, name))

    def _close_job(self):
        if self._job is not None:
            job = self._job
            self._job = None
            job.close()

    def _record_result(self, ok, missing, project_name):
        if ok:
            self._success += 1
        if missing:
            self._missing_by_project.setdefault(project_name, []).extend(missing)

    def _next_file(self):
        if self._index >= self._total:
            self._finish(False)
            return
        self._phase = self.PHASE_LOAD
        self._show_phase("加载")

    def Timer(self, msg):
        if self._finished:
            return
        if self._stop_requested:
            self._finish(True)
            return
        if self._index >= self._total:
            self._finish(False)
            return

        if self._phase == self.PHASE_LOAD:
            job = PackingJob(self._file_list[self._index])
            self._job = job
            try:
                loaded = job.load()
            except Exception as err:
                _log("[异常] {}: {}".format(job.file_name, err))
                _mark_failed_dir(job.target_dir)
                loaded = False
            if not loaded:
                self._job = None
                job.close()
                self._record_result(False, [], job.project_name)
                self._index += 1
                self._next_file()
                return
            self._phase = self.PHASE_SAVE
            self._show_phase("打包")
            return

        if self._phase == self.PHASE_SAVE:
            job = self._job
            try:
                ok = job.save()
                missing = job.missing_names()
            except Exception as err:
                _log("[异常] {}: {}".format(job.file_name, err))
                _mark_failed_dir(job.target_dir)
                ok = False
                missing = job.missing_names()
            self._record_result(ok, missing, job.project_name)
            self._index += 1
            if not ok:
                self._job = None
                job.close()
                self._next_file()
                return
            self._phase = self.PHASE_STAT
            self._show_phase("统计", self._index - 1)
            return

        if self._phase == self.PHASE_STAT:
            job = self._job
            try:
                job.finish()
            except Exception as err:
                _log("[异常] {}: {}".format(job.file_name, err))
                _mark_failed_dir(job.target_dir)
                if self._success > 0:
                    self._success -= 1
            self._job = None
            job.close()
            self._next_file()
            return

    def _finish(self, stopped):
        self._finished = True
        self.SetTimer(0)
        self._close_job()
        self.Close()
        c4d.StatusClear()
        c4d.EventAdd()

        missing_total = sum(len(v) for v in self._missing_by_project.values())
        if stopped:
            summary = "已停止！成功: {} / 已处理: {} / 总数: {}".format(
                self._success, self._index, self._total)
        else:
            summary = "处理完毕！成功: {} / 总数: {}".format(self._success, self._total)

        report_path = ""
        if missing_total:
            report_lines = [summary, "", "缺失资源（共 {} 个，按工程分组）:".format(missing_total)]
            for proj in sorted(self._missing_by_project):
                report_lines.append("")
                report_lines.append("[{}]".format(proj))
                for m in self._missing_by_project[proj]:
                    report_lines.append("    " + m)
            base_dir = os.path.dirname(self._file_list[0]) if self._file_list else ""
            if base_dir:
                report_path = os.path.join(base_dir, "打包报告.txt")
                try:
                    with codecs.open(report_path, "w", "utf-8") as f:
                        f.write("\n".join(report_lines))
                except OSError:
                    report_path = ""

        dlg_msg = summary
        failed = self._index - self._success
        if failed:
            dlg_msg += "\n失败: {} 个（半成品文件夹已改名「-打包失败」，检查无用后可手动删除）".format(failed)
        if missing_total:
            dlg_msg += "\n缺失资源: {} 个".format(missing_total)
            if report_path:
                dlg_msg += "\n\n报告已保存:\n{}".format(report_path)
        else:
            dlg_msg += "\n缺失资源: 0"
        gui.MessageDialog(dlg_msg)

    def Command(self, dialog_id, msg):
        if dialog_id == self.ID_STOP_BTN:
            self._stop_requested = True
            self.Enable(self.ID_STOP_BTN, False)
            self.SetString(self.ID_PROGRESS_TEXT, "正在停止...")
            return True
        return True

    def AskClose(self):
        if not self._finished:
            self._stop_requested = True
            self.Enable(self.ID_STOP_BTN, False)
            self.SetString(self.ID_PROGRESS_TEXT, "正在停止...")
            return True
        return False


# ==================== 打包入口 ====================
_progress_dlg = None


def _start_packing(files):
    global _progress_dlg
    if not files:
        gui.MessageDialog("没有选中任何工程文件。")
        return
    _log("-" * 40)
    _log("开始批量打包，共 {} 个工程".format(len(files)))
    _progress_dlg = ProgressDialog(files)
    _progress_dlg.Open(c4d.DLG_TYPE_ASYNC, defaultw=290, defaulth=110)


# ==================== 辅助函数 ====================
def get_c4d_files(directory):
    """扫描文件夹里的 .c4d（读取失败时弹提示）。扫描逻辑统一在 list_direct_c4d_files。"""
    try:
        return list_direct_c4d_files(directory)
    except OSError as err:
        gui.MessageDialog("无法读取文件夹:\n{}".format(err))
        return []


def build_load_flags():
    return c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS


def build_save_project_flags():
    flags = (c4d.SAVEPROJECT_ASSETS | c4d.SAVEPROJECT_SCENEFILE |
             c4d.SAVEPROJECT_DONTFAILONMISSINGASSETS)
    for attr in ("SAVEPROJECT_USEDOCUMENTNAMEASFILENAME",
                 "SAVEPROJECT_ASSETLINKS_COPY_FILEASSETS",
                 "SAVEPROJECT_ASSETLINKS_COPY_NODEASSETS"):
        if hasattr(c4d, attr):
            flags |= getattr(c4d, attr)
    return flags


def looks_like_empty_scene(src, packed):
    if not os.path.isfile(src) or not os.path.isfile(packed):
        return False
    return os.path.getsize(src) > 5 * 1024 * 1024 and os.path.getsize(packed) < 1024 * 1024


def list_direct_c4d_files(directory):
    if not os.path.isdir(directory):
        return []
    raw = [
        os.path.join(directory, n) for n in os.listdir(directory)
        if n.lower().endswith(".c4d") and os.path.isfile(os.path.join(directory, n))
    ]
    return sorted(raw, key=lambda f: _natural_sort_key(os.path.basename(f)))


def folder_size_mb(path):
    """计算文件夹总大小（MB）。"""
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / (1024.0 * 1024.0)


def _asset_name(item):
    """缺失资源条目 -> 文件名。正常是 dict，个别版本可能直接是字符串，做个兜底。"""
    if isinstance(item, dict):
        return str(item.get("filename", item))
    return str(item)


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _pick_packed_scene(target_dir, expected):
    """在输出文件夹里找打包出的工程文件：优先与源文件同名；
    找不到且有多个 .c4d 时取体积最大的（主工程一般最大），并打印警告。"""
    if os.path.exists(expected):
        return expected
    created = list_direct_c4d_files(target_dir)
    if not created:
        return None
    if len(created) > 1:
        biggest = max(created, key=_safe_size)
        _log("[警告] 输出目录有 {} 个 .c4d，取体积最大的: {}".format(
            len(created), os.path.basename(biggest)))
        return biggest
    return created[0]


def _mark_failed_dir(target_dir):
    """打包失败的半成品文件夹：改名为「-打包失败」后缀（只改名不删除，内容可人工检查）。"""
    if not target_dir or not os.path.isdir(target_dir):
        return
    new_dir = target_dir + "-打包失败"
    i = 1
    while os.path.exists(new_dir):
        new_dir = "{}-打包失败-{}".format(target_dir, i)
        i += 1
    try:
        os.rename(target_dir, new_dir)
        _log("[清理] 半成品文件夹已改名: {}".format(os.path.basename(new_dir)))
    except OSError:
        pass


class PackingJob(object):
    """一个工程的加载/保存中间状态，进度对话框按阶段分帧调用。"""

    def __init__(self, file_path):
        self.file_path = file_path
        self.file_name = os.path.basename(file_path)
        self.project_name = os.path.splitext(self.file_name)[0]
        self.base_dir = os.path.dirname(file_path)
        self.target_dir = None
        self.expected_scene_file = None
        self.doc = None
        self.assets = []
        self.missing_assets = []

    def prepare(self):
        target_dir = os.path.join(self.base_dir, self.project_name)
        suffix = 1
        while os.path.exists(target_dir):
            target_dir = os.path.join(self.base_dir, "{}-{}".format(self.project_name, suffix))
            suffix += 1
        self.target_dir = target_dir
        self.expected_scene_file = os.path.join(target_dir, self.file_name)

    def load(self):
        self.prepare()
        _log("[加载] {}".format(self.file_name))
        self.doc = documents.LoadDocument(_to_utf8(self.file_path), build_load_flags())
        if self.doc is None:
            _log("[失败] 无法打开: {}".format(self.file_path))
            return False
        return True

    def save(self):
        _log("[打包] {} -> {}".format(self.file_name, self.project_name))
        ok = documents.SaveProject(self.doc, build_save_project_flags(),
                                   _to_utf8(self.target_dir), self.assets, self.missing_assets)

        packed_scene_file = _pick_packed_scene(self.target_dir, self.expected_scene_file)

        if ok and packed_scene_file and looks_like_empty_scene(self.file_path, packed_scene_file):
            _log("[失败] 输出工程疑似空壳: {}".format(packed_scene_file))
            _mark_failed_dir(self.target_dir)
            return False

        if not (ok and packed_scene_file):
            _log("[失败] SaveProject 未生成工程文件: {}".format(self.expected_scene_file))
            _mark_failed_dir(self.target_dir)
            return False
        return True

    def finish(self):
        size_mb = folder_size_mb(self.target_dir)
        _log("[成功] {} -> {:.1f} MB".format(self.file_name, size_mb))
        names = self.missing_names()
        if names:
            _log("[缺失资源] {} 个".format(len(names)))
            for name in names:
                _log("    - {}".format(name))
        return names

    def missing_names(self):
        return [_asset_name(item) for item in self.missing_assets]

    def close(self):
        if self.doc is not None:
            doc = self.doc
            self.doc = None
            try:
                documents.KillDocument(doc)
            except Exception:
                _log(traceback.format_exc())


def process_file(file_path):
    job = PackingJob(file_path)
    try:
        if not job.load():
            return False, []
        if not job.save():
            return False, job.missing_names()
        return True, job.finish()
    except Exception as err:
        _log("[异常] {}: {}".format(job.file_name, err))
        _mark_failed_dir(job.target_dir)
        return False, job.missing_names()
    finally:
        job.close()


# ==================== 主入口 ====================
_main_dlg = None


def main():
    global _main_dlg
    # 重复执行脚本时先关掉旧窗口，避免堆出多个对话框
    if _main_dlg is not None:
        try:
            _main_dlg.Close()
        except Exception:
            pass
    _main_dlg = BatchPackProjectsDialog()
    _main_dlg.Open(c4d.DLG_TYPE_ASYNC, defaultw=300, defaulth=500)


if __name__ == "__main__":
    main()
