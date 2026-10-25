# -*- coding: utf-8 -*-
"""
C盘空间扫描器（树形）

功能：
  1. 扫描磁盘目录，列出每个文件/文件夹的占用大小，自动以 KB / MB / GB 显示
  2. 打开后可在顶栏选择扫描哪个盘，默认 C 盘；单击目录即可展开/收起
  3. 文件为黑色；文件夹按大小上色：≤100MB 蓝、100MB+ 金、512MB+ 橙、1GB+ 红
     文件夹以加粗区分于文件

用法：
  双击运行或在终端执行:  python disk_scan.py [起始路径]    默认 C:\
  命令行快速查看:         python disk_scan.py --list [路径] 不弹窗口，直接列出

性能要点：
  统计目录大小用 scan_once() 一次遍历，把整棵子树里每个目录的大小都记进索引，
  因此同一个目录只会被枚举一次；之后再展开它的子目录直接查表，零磁盘操作。
"""
import json
import os
import queue
import shutil
import string
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import Future, as_completed
from tkinter import messagebox, ttk

# 并行统计目录大小的线程数（磁盘 I/O 密集，实测 16 左右收益最好）
MAX_WORKERS = min(32, (os.cpu_count() or 4) * 2)

# 单个目录最多往界面上塞多少个子项（防止 WinSxS 这类超大目录卡死界面）
MAX_SUBITEMS = 500

# 轮询间隔与单次最多处理的消息数（避免超大目录一次刷爆界面线程）
POLL_MS = 120
MAX_PER_POLL = 2000

# 子目录数量超过这个值时按块并行，减少手工任务调度的开销
BLOCK_MIN = 32

# Windows 文件属性位：目录 / 重解析点（符号链接、junction 都算）
ATTR_DIR = 0x10
ATTR_REPARSE = 0x400


def is_reparse(st) -> bool:
    """是否为重解析点（符号链接或 junction）。

    注意：Windows 的 junction（目录联接）用 os.DirEntry.is_symlink() 判断会返回
    False，pnpm 的 node_modules 正是靠 junction 互指，不按属性位拦会无限递归。"""
    return bool(getattr(st, "st_file_attributes", 0) & ATTR_REPARSE)


def is_link_path(path: str) -> bool:
    """路径本身是否是符号链接 / junction（不跟随）"""
    try:
        return is_reparse(os.lstat(path))
    except OSError:
        return False


def scan_block(paths):
    """把若干互不重叠的目录串行扫完，返回合并后的 {目录: 大小}"""
    merged = {}
    for p in paths:
        merged.update(scan_once(p))
    return merged


class _ScanPool:
    """固定 N 个 daemon 线程的简易线程池，返回 Future，进程退出时不阻塞"""

    def __init__(self, n: int):
        self.tasks = queue.Queue()
        for _ in range(n):
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            fn, args, fut = self.tasks.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:      # 线程池不能把异常吞掉
                fut.set_exception(e)

    def submit(self, fn, *args):
        fut = Future()
        self.tasks.put((fn, args, fut))
        return fut


# 全局共享线程池：所有展开目录的子目录大小都交给它并行算
_POOL = _ScanPool(MAX_WORKERS)


def list_drives():
    """列出所有可用盘符，如 ['C:\\', 'D:\\']"""
    drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    return drives or ["C:\\"]


# ---------- 扫描缓存 ----------
# 缓存文件与本脚本同目录，保存"扫过的目录 -> (大小, 修改时间)"，下次启动秒开
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
CACHE_VERSION = 2
MAX_CACHE_DIRS = 400000     # 缓存条目上限，超出后不写盘，避免生成超大文件


def dir_mtime(path: str):
    """返回目录/文件的修改时间戳；失败（不存在/无权限）返回 None"""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def load_cache():
    """读取缓存文件；缺失、损坏或版本不符时返回空结构，由程序重扫重建"""
    empty = {"version": CACHE_VERSION, "dirs": {}, "stamps": {}}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    # 旧版（v1）缓存是 {路径: {mtime, size, children}}，能直接迁移过来
    if data.get("version") == 1:
        dirs, stamps = {}, {}
        for p, e in (data.get("dirs") or {}).items():
            if isinstance(e, dict) and isinstance(e.get("size"), int):
                dirs[p] = e["size"]
                if e.get("mtime") is not None:
                    stamps[p] = e["mtime"]
        return {"version": CACHE_VERSION, "dirs": dirs, "stamps": stamps}
    if data.get("version") != CACHE_VERSION:
        return empty
    dirs, stamps = data.get("dirs"), data.get("stamps")
    if not isinstance(dirs, dict) or not isinstance(stamps, dict):
        return empty
    return {"version": CACHE_VERSION, "dirs": dirs, "stamps": stamps}


def save_cache(cache):
    """把缓存写入文件（容错：写失败不打断程序）"""
    if len(cache["dirs"]) > MAX_CACHE_DIRS:
        return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    except OSError:
        pass


def format_size(num: float) -> str:
    """把字节数格式化成 KB/MB/GB 等单位"""
    if num < 1024:
        return f"{num:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024:
            return f"{num:.1f} {unit}"
    return f"{num:.1f} PB"


def scan_once(root: str) -> dict:
    """一次遍历统计 root 及其所有子孙目录的字节数，返回 {目录路径: 字节数}。

    用显式栈代替递归，每个目录只 scandir 一次；重解析点（符号链接/junction）
    一律跳过，避免重复统计或死循环。整棵子树扫完后，展开任意子目录都能直接
    查表，不用再碰磁盘。"""
    sizes = {}
    try:
        it = os.scandir(root)
    except OSError:
        sizes[root] = 0
        return sizes
    stack = [[root, it, 0]]          # [路径, 目录迭代器, 已累计字节]
    while stack:
        frame = stack[-1]
        path, it = frame[0], frame[1]
        try:
            entry = next(it)
        except StopIteration:
            it.close()
            sizes[path] = frame[2]
            stack.pop()
            if stack:
                stack[-1][2] += frame[2]
            continue
        except OSError:
            it.close()
            sizes[path] = frame[2]
            stack.pop()
            if stack:
                stack[-1][2] += frame[2]
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if is_reparse(st):
            continue                      # 符号链接 / junction：一律不进入
        attr = getattr(st, "st_file_attributes", 0)
        try:
            is_dir = bool(attr & ATTR_DIR) if attr else \
                entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if not is_dir:
            frame[2] += st.st_size
            continue
        try:
            stack.append([entry.path, os.scandir(entry.path), 0])
        except OSError:
            continue
    return sizes


def list_children(path: str):
    """返回某个目录的直接子项，每个元素为 (名称, 绝对路径, 大小, 是否目录, 是否软链接)。
    文件立即取大小，目录大小填 0 由调用方再算；软链接用 is_link 标记。
    返回 (子项列表, 实际条目总数)。"""
    try:
        with os.scandir(path) as it:
            entries = [e for e in it]
    except (PermissionError, OSError):
        return [], 0
    total = len(entries)
    result = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except (PermissionError, OSError):
            continue
        attr = getattr(st, "st_file_attributes", 0)
        is_link = is_reparse(st) or e.is_symlink()
        try:
            is_dir = bool(attr & ATTR_DIR) if attr else \
                e.is_dir(follow_symlinks=False)
        except (PermissionError, OSError):
            continue
        size = 0 if is_dir else st.st_size
        result.append((e.name, e.path, size, is_dir, is_link))
    return result, total


class DiskScanner:
    """树形 GUI：单击目录懒加载扫描下一级"""

    def __init__(self, root_path: str):
        self.root_path = os.path.abspath(root_path)
        self.queue = queue.Queue()
        self.closing = False
        self.scanning = {}      # 正在扫描的节点 iid -> 占位节点 iid
        self.done = set()       # 已完成扫描的节点 iid（含空目录，避免重复扫）
        self.sizes = {}         # 节点 iid -> 占用字节数（界面显示用）
        self.loading = {}       # 节点 iid -> 占位节点 iid
        self.is_dir = set()     # 目录节点集合
        self.index = {}         # 目录路径 -> 字节数（一次遍历建的全量索引）
        self.fresh = set()      # 本次运行已确认有效的索引路径（不再校验）
        self.cache = load_cache()                 # 磁盘扫描缓存（内存态）
        self.cache_dirs = self.cache["dirs"]
        self.stamps = self.cache["stamps"]
        self.index.update(self.cache_dirs)        # 上次扫过的目录直接可用

        self.win = tk.Tk()
        self.win.title("磁盘空间扫描器 - 小鱼出品")
        self.win.geometry("840x600")
        # 界面字体整体放大一点
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=f.cget("size") + 1)
            except tk.TclError:
                pass

        # 顶栏：盘符选择 + 状态 + 重扫按钮
        top = ttk.Frame(self.win, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="扫描盘:").pack(side="left")
        self.drive_var = tk.StringVar(value=self.root_path)
        self.drive_box = ttk.Combobox(top, textvariable=self.drive_var,
                                      values=list_drives(), width=6, state="readonly")
        self.drive_box.pack(side="left", padx=(0, 8))
        self.drive_box.bind("<<ComboboxSelected>>", self._on_drive_change)
        self.status = tk.StringVar(value=f"就绪，根目录：{self.root_path}（单击目录展开/收起）")
        ttk.Label(top, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="重新扫描当前目录", command=self._rescan).pack(side="right")

        # 树形列表
        frame = ttk.Frame(self.win)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(frame, show="tree")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 加粗与颜色分开两个 tag，避免展开重绘时颜色被覆盖
        _f = tkfont.nametofont("TkDefaultFont")
        self.tree.tag_configure("dirfont",
                                font=(_f.cget("family"), _f.cget("size"), "bold"))
        self.tree.tag_configure("blue", foreground="#1f5fbf")
        self.tree.tag_configure("red", foreground="#d33000")
        self.tree.tag_configure("orange", foreground="#e67e22")
        self.tree.tag_configure("gold", foreground="#b8860b")
        self.tree.tag_configure("link", foreground="#00ff00")   # 软链接：荧光绿
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._toggle)
        self.tree.bind("<Double-1>", self._toggle)
        self.tree.bind("<Button-3>", self._on_right_click)

        # 插入根节点并自动展开扫描
        self.root_iid = self.root_path
        self.tree.insert("", "end", iid=self.root_iid, text=self.root_iid, open=True,
                         tags=self._dir_tags(None))
        self.is_dir.add(self.root_iid)
        self.tree.selection_set(self.root_iid)
        self._start_scan(self.root_iid)

        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.win.after(POLL_MS, self._poll)

    # ---------- 界面显示 ----------
    def _dir_tags(self, size):
        """目录配色：加粗 + 按大小上色（≤100MB 蓝，100MB+ 金，512MB+ 橙，1GB+ 红）"""
        color = "blue"
        if size is not None:
            if size >= 1 << 30:
                color = "red"
            elif size >= 512 << 20:
                color = "orange"
            elif size >= 100 << 20:
                color = "gold"
        return ("dirfont", color)

    def _dir_text(self, path: str) -> str:
        """目录节点文本：显示占用或『计算中…』"""
        size = self.sizes.get(path)
        size_txt = format_size(size) if size is not None else "计算中…"
        name = os.path.basename(path) or path
        return f"{name}   [{size_txt}]"

    def _file_text(self, path: str) -> str:
        """文件节点文本：显示占用大小（文件始终黑色）"""
        name = os.path.basename(path) or path
        return f"{name}   [{format_size(self.sizes.get(path, 0))}]"

    # ---------- 扫描流程 ----------
    def _start_scan(self, iid: str, force: bool = False):
        """为一个目录节点启动扫描。force=True 强制重扫（忽略索引与缓存）"""
        if not force:
            if iid in self.scanning or iid in self.done:
                return
            if self.tree.get_children(iid):
                return
        ph = self.tree.insert(iid, "end", iid=iid + "\x00loading", text="正在扫描…")
        self.scanning[iid] = ph
        self.loading[iid] = ph
        threading.Thread(target=self._worker, args=(iid,), daemon=True).start()

    def _lookup(self, path: str):
        """查目录大小：本次运行算过的直接用；上次缓存的要 mtime 没变才用"""
        if path in self.fresh:
            return self.index.get(path)
        size = self.index.get(path)
        if size is not None and self.stamps.get(path) == dir_mtime(path):
            self.fresh.add(path)
            return size
        return None

    def _worker(self, iid: str):
        """后台线程：先快列子项，再交给共享线程池并行算各子目录大小。

        每个子目录用 scan_once 一次遍历，顺带把它整棵子树的大小都写进索引，
        所以再展开它的子目录时直接命中，不会重复扫磁盘。"""
        if self.closing:
            return
        listing, total = list_children(iid)
        if self.closing:
            return
        self.queue.put(("listing", iid, listing, total))
        todo = []
        for _, path, _, is_dir, is_link in listing:
            if not is_dir:
                continue
            if is_link:
                # 指向别处的重解析点：不进去统计（避免重复统计和死循环）
                self.queue.put(("sized", iid, path, 0))
                continue
            size = self._lookup(path)
            if size is None:
                todo.append(path)
            else:
                self.queue.put(("sized", iid, path, size))
        if not todo:
            self.queue.put(("all_done", iid))
            return
        if len(todo) >= BLOCK_MIN:
            # 子目录很多：切成若干块，每块一个任务，避免几千个任务抢 GIL
            n = min(MAX_WORKERS, max(1, len(todo) // 8))
            jobs = {_POOL.submit(scan_block, todo[i::n]): todo[i::n]
                    for i in range(n)}
        else:
            jobs = {_POOL.submit(scan_once, p): p for p in todo}
        for fut in as_completed(list(jobs)):
            target = jobs[fut]
            paths = [target] if isinstance(target, str) else target
            try:
                part = fut.result()
            except Exception:
                part = {p: 0 for p in paths}
            self.index.update(part)
            self.fresh.update(part)
            for p in paths:
                self.stamps[p] = dir_mtime(p)
                if not self.closing:
                    self.queue.put(("sized", iid, p, part.get(p, 0)))
        self.queue.put(("all_done", iid))

    def _on_open(self, event):
        iid = self.tree.focus()
        if iid not in self.is_dir:
            return
        if iid in self.scanning:
            return
        if self.tree.get_children(iid):
            self._resort(iid)   # 已有子节点：按最新大小重排（点箭头展开也生效）
            return
        self._start_scan(iid)

    def _toggle(self, event):
        """单击/双击目录：展开或收起（点中原生箭头则交给默认行为）"""
        elem = self.tree.identify_element(event.x, event.y) or ""
        if "indicator" in elem:
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.is_dir:
            return
        opened = not self.tree.item(iid, "open")
        self.tree.item(iid, open=opened)
        if opened:
            if self.tree.get_children(iid):
                self._resort(iid)   # 已有子节点：按最新大小重排（折叠再展开也生效）
            else:
                self._start_scan(iid)
        # 展开/收起后重刷一次颜色，规避 ttk 重绘丢色
        self.tree.item(iid, tags=self._dir_tags(self.sizes.get(iid)))

    def _rescan(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid not in self.is_dir:
            return
        # 清理可能的旧扫描状态，允许强制重新扫描
        ph = self.loading.pop(iid, None)
        if ph and self.tree.exists(ph):
            self.tree.delete(ph)
        self.scanning.pop(iid, None)
        self.done.discard(iid)
        # 删除子节点前，先递归清理整个子树的扫描状态，
        # 避免旧 done 残留导致节点重建后子文件夹点不开
        for kid in self.tree.get_children(iid):
            self._purge_node_state(kid)
            self.tree.delete(kid)
        # 强制重扫：让本目录及子孙的索引失效
        prefix = iid.rstrip("\\") + "\\"
        for p in [p for p in self.index if p == iid or p.startswith(prefix)]:
            self.index.pop(p, None)
            self.fresh.discard(p)
            self.stamps.pop(p, None)
            self.cache_dirs.pop(p, None)
        self._start_scan(iid, force=True)

    def _purge_node_state(self, iid: str):
        """递归清除某节点及其所有后代的扫描状态（done/scanning/loading/sizes），
        用于重扫删除节点后，避免旧状态残留导致子文件夹点不开。"""
        for kid in self.tree.get_children(iid):
            self._purge_node_state(kid)
        self.done.discard(iid)
        self.scanning.pop(iid, None)
        self.loading.pop(iid, None)
        self.sizes.pop(iid, None)

    # ---------- 盘符切换 ----------
    def _on_drive_change(self, event=None):
        self._switch_root(self.drive_var.get())

    def _switch_root(self, new_root: str):
        """清空当前树，切换到指定盘重新扫描"""
        if not os.path.isdir(new_root):
            return
        for kid in self.tree.get_children(""):
            self.tree.delete(kid)
        self.sizes.clear()
        self.is_dir.clear()
        self.done.clear()
        self.scanning.clear()
        self.loading.clear()
        self.root_path = new_root
        self.root_iid = new_root
        self.drive_var.set(new_root)
        self.tree.insert("", "end", iid=new_root, text=new_root, open=True,
                         tags=self._dir_tags(None))
        self.is_dir.add(new_root)
        self.status.set(f"就绪，根目录：{new_root}（单击目录展开/收起）")
        self._start_scan(new_root)

    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        text = self.tree.item(iid, "text")
        self.status.set(f"{iid}    {text}")

    # ---------- 右键菜单 ----------
    def _on_right_click(self, event):
        """右键弹出菜单：复制路径 / 跳转位置 / 删除"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="复制完整路径", command=lambda: self._copy_path(iid))
        menu.add_command(label="跳转到文件夹位置", command=lambda: self._reveal(iid))
        # 软链接/junction 额外提供"跳转到链接目标"
        if is_link_path(iid):
            menu.add_command(label="跳转到链接目标位置", command=lambda: self._reveal_target(iid))
        menu.add_separator()
        menu.add_command(label="删除", command=lambda: self._delete_node(iid))
        menu.add_command(label="清除缓存", command=lambda: self._clear_cache_node(iid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_path(self, iid):
        """把节点完整路径复制到剪贴板"""
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(iid)
            self.status.set(f"已复制完整路径：{iid}")
        except tk.TclError:
            self.status.set("复制到剪贴板失败")

    def _reveal(self, iid):
        """文件夹直接打开进入；文件则打开所在目录并选中它"""
        try:
            if iid in self.is_dir:
                os.startfile(iid)
            else:
                subprocess.Popen(["explorer", f"/select,{iid}"])
            self.status.set(f"已打开：{iid}")
        except (OSError, AttributeError) as e:
            self.status.set(f"打开失败：{e}")

    def _reveal_target(self, iid):
        """打开软链接指向的目标（目录直接打开，文件则选中它）"""
        target = os.path.realpath(iid)
        try:
            if os.path.isdir(target):
                os.startfile(target)
            else:
                subprocess.Popen(["explorer", f"/select,{target}"])
            self.status.set(f"已打开链接目标：{target}")
        except (OSError, AttributeError) as e:
            self.status.set(f"打开失败：{e}")

    def _delete_node(self, iid):
        """右键删除：先弹确认框防误删；确认后删除文件/文件夹，
        权限不足则提示并终止，不继续。"""
        # 保护：根目录不可删除
        if iid == self.root_path:
            messagebox.showerror("无法删除", "不能删除根目录。")
            return
        # 保护：C 盘（系统盘）中的文件一律不可删除
        drive, _ = os.path.splitdrive(iid)
        if drive.upper() == "C:":
            messagebox.showerror(
                "无法删除",
                "出于安全考虑，不能删除 C 盘中的文件。")
            return
        # 确认防误删
        name = os.path.basename(iid) or iid
        kind = "文件夹" if iid in self.is_dir else "文件"
        if not messagebox.askyesno(
                "确认删除",
                f"确定要永久删除这个{kind}吗？\n\n{name}\n\n"
                "此操作不可撤销，且不会进入回收站！"):
            return
        # 执行删除；权限不够（占用/只读/无权限）直接终止
        try:
            if iid in self.is_dir:
                shutil.rmtree(iid)      # 递归删除整个文件夹
            else:
                os.remove(iid)
        except (PermissionError, OSError) as e:
            messagebox.showerror(
                "删除失败",
                f"没有权限删除：\n{e}\n\n"
                "可能原因：文件被占用、只读或需要管理员权限。\n已终止，未删除。")
            return
        # 删除成功：从界面移除节点、清缓存项，并刷新父目录大小与排序
        parent = os.path.dirname(iid)
        self.tree.delete(iid)
        self.sizes.pop(iid, None)
        self.cache_dirs.pop(iid, None)
        self.stamps.pop(iid, None)
        self.index.pop(iid, None)
        self.fresh.discard(iid)
        self.is_dir.discard(iid)
        self.done.discard(iid)
        self.scanning.pop(iid, None)
        self.loading.pop(iid, None)
        if self.tree.exists(parent):
            self._update_size_chain(parent)
        self.status.set(f"已删除：{iid}")

    def _clear_cache_node(self, iid):
        """清除选中目录及其所有子目录的扫描缓存并保存磁盘，
        下次展开这些目录会重新扫描。"""
        if not messagebox.askyesno(
                "清除缓存",
                f"确定要清除 {iid} 及其子目录的扫描缓存吗？\n\n"
                "清除后，下次展开这些目录将重新扫描。"):
            return
        prefix = iid.rstrip("\\") + "\\"
        removed = [p for p in list(self.cache_dirs)
                   if p == iid or p.startswith(prefix)]
        for p in removed:
            self.cache_dirs.pop(p, None)
            self.stamps.pop(p, None)
            self.index.pop(p, None)
            self.fresh.discard(p)
        save_cache(self.cache)
        self.status.set(f"已清除 {len(removed)} 个目录的缓存（下次展开会重新扫描）")

    # ---------- 消息处理 ----------
    def _poll(self):
        if self.closing:
            return
        for _ in range(MAX_PER_POLL):
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            self._handle(msg)
        self._after_id = self.win.after(POLL_MS, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "listing":
            _, iid, listing, total = msg
            if not self.tree.exists(iid):   # 可能已切换盘符
                return
            self._remove_loading(iid)
            for name, path, size, is_dir, is_link in listing[:MAX_SUBITEMS]:
                if is_dir:
                    self.is_dir.add(path)
                    label = self._dir_text(path)
                    # 重解析点目录标绿，提示它不参与统计
                    tags = ("dirfont", "link") if is_link else self._dir_tags(None)
                elif is_link:
                    self.sizes[path] = size
                    label = self._file_text(path)
                    tags = ("link",)   # 软链接：荧光绿
                else:
                    self.sizes[path] = size
                    label = self._file_text(path)
                    tags = ()   # 普通文件始终黑色
                self.tree.insert(iid, "end", iid=path, text=label, tags=tags)
            # 超大目录截断提示（WinSxS 这类有数万子项的目录）
            if total > MAX_SUBITEMS:
                self.tree.insert(
                    iid, "end", iid=iid + "\x00truncated",
                    text=f"…共 {total} 个子项，超大目录已截断，仅显示前 {MAX_SUBITEMS} 项",
                    tags=())
        elif kind == "sized":
            _, iid, path, size = msg
            self.sizes[path] = size
            if self.tree.exists(path):
                self.tree.item(path, text=self._dir_text(path), tags=self._dir_tags(size))
            # 子目录大小全算完时，父目录大小也能定下来了
            self.cache_dirs[path] = size
            self.cache["dirs"] = self.cache_dirs
        elif kind == "all_done":
            iid = msg[1]
            if not self.tree.exists(iid):
                self.scanning.pop(iid, None)
                return
            self._remove_loading(iid)
            self.scanning.pop(iid, None)
            self.done.add(iid)
            self._resort(iid)
            self.status.set(f"完成：{iid}，共 {len(self.tree.get_children(iid))} 项")
            # 扫描/重扫完成后，向上刷新本目录及所有祖先的大小
            self._update_size_chain(iid)

    def _remove_loading(self, iid: str):
        """删除『正在扫描…』占位节点（scanning 标记保留到 all_done 才清除，
        避免空窗期内重复展开导致重复扫描）"""
        ph = self.loading.pop(iid, None)
        if ph and self.tree.exists(ph):
            self.tree.delete(ph)

    def _resort(self, iid: str):
        """把子节点按占用大小从大到小重排，大头一眼可见"""
        if not self.tree.exists(iid):
            return
        kids = [k for k in self.tree.get_children(iid)]
        kids.sort(key=lambda k: self.sizes.get(k, 0), reverse=True)
        for idx, kid in enumerate(kids):
            self.tree.move(kid, iid, idx)

    def _recompute_dir_size(self, path: str) -> int:
        """用直接子项累加目录总大小（子目录大小取已算好的值，不递归，避免全盘重扫）。"""
        listing, total = list_children(path)
        result = 0
        for _, p, size, is_dir, _ in listing:
            if is_dir:
                result += self.sizes.get(p, 0)
            else:
                result += size
        return result

    def _update_size_chain(self, iid: str):
        """从 iid 沿父链向上，逐个重新累加并刷新目录大小，同时让每个
        目录在其父目录的子列表里按最新大小重新排序。
        解决"重扫某目录后，它自身及祖先大小不更新、排序不变"的问题。"""
        cur = iid
        while True:
            total = self._recompute_dir_size(cur)
            self.sizes[cur] = total
            if self.tree.exists(cur):
                self.tree.item(cur, text=self._dir_text(cur),
                               tags=self._dir_tags(total))
            parent = os.path.dirname(cur)
            # cur 的大小变了，在父目录的子列表里按新大小重排
            if parent != cur and self.tree.exists(parent):
                self._resort(parent)
            if parent == cur:
                break
            cur = parent

    def _close(self):
        self.closing = True
        try:
            self.win.after_cancel(self._after_id)
        except (tk.TclError, AttributeError):
            pass
        self.cache["dirs"] = self.cache_dirs
        self.cache["stamps"] = self.stamps
        save_cache(self.cache)
        self.win.destroy()

    def run(self):
        self.win.mainloop()


def cli_list(path: str):
    """命令行模式：列出指定目录的子项大小，用于快速查看或自测"""
    # 修复 cmd 传参问题：带引号且以反斜杠结尾的参数（如 "C:\"）会被 cmd
    # 转义成 C:"（丢反斜杠、尾部多个引号）。这里去掉残留引号；
    # 裸盘符（C:）补上反斜杠，指向根目录而非"当前目录"。
    path = path.rstrip('"')
    if len(path) == 2 and path[1] == ':':
        path += '\\'
    items, _ = list_children(path)
    # 各子目录并行统计，一次遍历就把整棵子树的大小都算出来
    jobs = {_POOL.submit(scan_once, p): p
            for _, p, _, is_dir, _ in items if is_dir}
    found = {}
    for fut in as_completed(list(jobs)):
        try:
            found.update(fut.result())
        except Exception:
            pass
    total = 0
    for name, p, size, is_dir, is_link in items:
        if is_dir:
            size = found.get(p, 0)
        total += size
        tag = "[链接]" if is_link else ("[目录]" if is_dir else "      ")
        print(f"{format_size(size):>12}  {tag}  {name}")
    print("-" * 40)
    print(f"共 {len(items)} 项，总计 {format_size(total)}")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--list":
        cli_list(args[1] if len(args) > 1 else "C:\\")
        return
    try:
        import tkinter
    except ImportError:
        print("错误：当前 Python 没有 tkinter 组件，请安装完整版 Python（python.org 下载）后再运行。")
        sys.exit(1)
    root = args[0] if args else "C:\\"
    DiskScanner(root).run()


if __name__ == "__main__":
    main()
