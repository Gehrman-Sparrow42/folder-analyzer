"""Iterative directory scanner backed by a temporary SQLite index."""

from __future__ import annotations

import csv
import ctypes
import io
import os
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path


class ScanManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.db_path: str | None = None
        self.started = 0.0
        self.finished = 0.0
        self.status = self._blank_status()

    @staticmethod
    def _blank_status() -> dict:
        return {"state": "idle", "root": "", "files": 0, "folders": 0, "total_bytes": 0,
                "skipped": 0, "elapsed": 0.0, "current_path": "", "error": ""}

    def snapshot(self) -> dict:
        with self.lock:
            result = self.status.copy()
            if result["state"] == "scanning":
                result["elapsed"] = round(time.monotonic() - self.started, 2)
            return result

    def start(self, root: str) -> dict:
        if not isinstance(root, str) or not root.strip():
            raise ValueError("Choose a folder to scan.")
        root = os.path.abspath(os.path.expanduser(root.strip()))
        if not os.path.isdir(root):
            raise ValueError("The selected folder does not exist or cannot be opened.")
        if _is_link(root):
            raise ValueError("Linked folders and junctions cannot be scan roots.")
        with self.lock:
            if self.status["state"] == "scanning":
                raise RuntimeError("A scan is already in progress.")
            previous = self.db_path
            self.db_path = None
            self.cancel_event.clear()
            self.started = time.monotonic()
            self.finished = 0.0
            self.status = self._blank_status()
            self.status.update(state="scanning", root=root, current_path=root)
            self.thread = threading.Thread(target=self._scan, args=(root,), daemon=True)
            self.thread.start()
        if previous:
            try:
                os.unlink(previous)
            except OSError:
                pass
        return self.snapshot()

    def cancel(self) -> dict:
        with self.lock:
            if self.status["state"] == "scanning":
                self.cancel_event.set()
            return self.snapshot()

    def trash_folder(self, path: str) -> dict:
        """Send one indexed child directory to the Windows Recycle Bin."""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Choose a scanned folder to move to the Recycle Bin.")
        with self.lock:
            if self.status["state"] == "scanning":
                raise RuntimeError("Wait for the scan to finish before moving a folder.")
            if self.status["state"] not in ("complete", "cancelled") or not self.db_path:
                raise RuntimeError("Scan a folder first.")
            root = self.status["root"]
            target = os.path.abspath(path)
            root_canonical = os.path.normcase(os.path.realpath(root))
            target_canonical = os.path.normcase(os.path.realpath(target))
            if target_canonical == root_canonical or os.path.commonpath((root_canonical, target_canonical)) != root_canonical:
                raise ValueError("Only indexed folders inside the scan root can be moved.")
            if not os.path.isdir(target) or _is_link(target):
                raise ValueError("The folder is missing, linked, or a junction.")
            with closing(self._connect()) as db:
                cursor = target
                while True:
                    row = db.execute("SELECT kind,device,inode FROM entries WHERE path=?", (cursor,)).fetchone()
                    if row is None or row["kind"] != "folder":
                        raise ValueError("The folder is not in the scan index.")
                    info = os.stat(cursor, follow_symlinks=False)
                    if _is_link(cursor) or (str(info.st_dev), str(info.st_ino)) != (row["device"], row["inode"]):
                        raise ValueError("A scanned folder changed since the scan; scan again first.")
                    if cursor == root:
                        break
                    cursor = os.path.dirname(cursor)
            _recycle_folder(target)
            scan = self.start(root)
            return {"status": "trashed", "path": target, "scan": scan}

    def _scan(self, root: str) -> None:
        db_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="folder_analyzer_", suffix=".sqlite3", delete=False) as tmp:
                db_path = tmp.name
            db = sqlite3.connect(db_path)
            try:
                db.executescript("""
                    CREATE TABLE entries (
                        path TEXT PRIMARY KEY, parent TEXT, name TEXT NOT NULL,
                        kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                        files INTEGER NOT NULL DEFAULT 0, folders INTEGER NOT NULL DEFAULT 0,
                        modified REAL, extension TEXT NOT NULL DEFAULT '',
                        device TEXT, inode TEXT
                    );
                    CREATE INDEX by_parent ON entries(parent);
                    CREATE INDEX by_kind_extension ON entries(kind, extension);
                    CREATE TABLE errors (path TEXT NOT NULL, error TEXT NOT NULL);
                """)
                self._walk(db, root)
                db.commit()
            finally:
                db.close()
            with self.lock:
                self.db_path = db_path
                self.finished = time.monotonic()
                self.status["elapsed"] = round(self.finished - self.started, 2)
                self.status["current_path"] = ""
                self.status["state"] = "cancelled" if self.cancel_event.is_set() else "complete"
        except Exception as exc:  # noqa: BLE001 - worker boundary records unexpected failures
            if db_path:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass
            with self.lock:
                self.finished = time.monotonic()
                self.status.update(state="error", error=str(exc), current_path="",
                                   elapsed=round(self.finished - self.started, 2))

    def _walk(self, db: sqlite3.Connection, root: str) -> None:
        root_stat = os.stat(root)
        db.execute("INSERT INTO entries(path,parent,name,kind,modified,device,inode) VALUES (?,?,?,?,?,?,?)",
                   (root, None, os.path.basename(root) or root, "folder", root_stat.st_mtime,
                    str(root_stat.st_dev), str(root_stat.st_ino)))
        # Exit markers compute directory totals after all descendants have been inserted.
        stack: list[tuple[str, bool]] = [(root, False)]
        pending = 0
        while stack:
            if self.cancel_event.is_set():
                break
            folder, exiting = stack.pop()
            if exiting:
                db.execute("""UPDATE entries SET
                    size=(SELECT COALESCE(SUM(size),0) FROM entries WHERE parent=?),
                    files=(SELECT COALESCE(SUM(files),0) FROM entries WHERE parent=?)
                          +(SELECT COUNT(*) FROM entries WHERE parent=? AND kind='file'),
                    folders=(SELECT COALESCE(SUM(folders),0) FROM entries WHERE parent=?)
                            +(SELECT COUNT(*) FROM entries WHERE parent=? AND kind='folder')
                    WHERE path=?""", (folder, folder, folder, folder, folder, folder))
                continue
            with self.lock:
                self.status["current_path"] = folder
            stack.append((folder, True))
            try:
                with os.scandir(folder) as iterator:
                    for entry in iterator:
                        if self.cancel_event.is_set():
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if entry.is_symlink() or _is_reparse_stat(info):
                                self._record_error(db, entry.path, OSError("Linked or reparse item skipped."))
                                continue
                            is_dir = stat.S_ISDIR(info.st_mode)
                            if not is_dir and not stat.S_ISREG(info.st_mode):
                                continue
                            kind = "folder" if is_dir else "file"
                            size = 0 if is_dir else info.st_size
                            extension = Path(entry.name).suffix.lower() if not is_dir else ""
                            identity = os.stat(entry.path, follow_symlinks=False) if is_dir else info
                            db.execute("""INSERT INTO entries
                                (path,parent,name,kind,size,modified,extension,device,inode)
                                VALUES (?,?,?,?,?,?,?,?,?)""",
                                (entry.path, folder, entry.name, kind, size, info.st_mtime, extension,
                                 str(identity.st_dev), str(identity.st_ino)))
                            if is_dir:
                                stack.append((entry.path, False))
                            with self.lock:
                                self.status["folders" if is_dir else "files"] += 1
                                if not is_dir:
                                    self.status["total_bytes"] += size
                            pending += 1
                            if pending >= 500:
                                db.commit()
                                pending = 0
                        except OSError as exc:
                            self._record_error(db, entry.path, exc)
            except OSError as exc:
                self._record_error(db, folder, exc)
        # On cancellation, reconcile every entered directory in depth order so the
        # partial index remains internally consistent and can still be explored.
        if self.cancel_event.is_set():
            folders = db.execute("SELECT path FROM entries WHERE kind='folder' ORDER BY LENGTH(path) DESC").fetchall()
            for (folder,) in folders:
                db.execute("""UPDATE entries SET
                    size=(SELECT COALESCE(SUM(size),0) FROM entries WHERE parent=?),
                    files=(SELECT COALESCE(SUM(files),0) FROM entries WHERE parent=?)
                          +(SELECT COUNT(*) FROM entries WHERE parent=? AND kind='file'),
                    folders=(SELECT COALESCE(SUM(folders),0) FROM entries WHERE parent=?)
                            +(SELECT COUNT(*) FROM entries WHERE parent=? AND kind='folder')
                    WHERE path=?""", (folder, folder, folder, folder, folder, folder))

    def _record_error(self, db: sqlite3.Connection, path: str, exc: OSError) -> None:
        db.execute("INSERT INTO errors(path,error) VALUES (?,?)", (path, str(exc)))
        with self.lock:
            self.status["skipped"] += 1

    def _connect(self) -> sqlite3.Connection:
        with self.lock:
            if self.status["state"] == "scanning":
                raise RuntimeError("The scan is still running.")
            if not self.db_path:
                raise LookupError("Scan a folder first.")
            db = sqlite3.connect(f"file:{Path(self.db_path).as_posix()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            return db

    def browse(self, path: str | None, sort: str, order: str, offset: int, limit: int) -> dict:
        with self.lock, closing(self._connect()) as db:
            root = self.snapshot()["root"]
            path = os.path.abspath(path) if path else root
            row = db.execute("SELECT parent FROM entries WHERE path=? AND kind='folder'", (path,)).fetchone()
            if row is None:
                raise LookupError("Folder is outside the scanned index or was not scanned.")
            sort_column = "size" if sort == "size" else "name COLLATE NOCASE"
            direction = "DESC" if order == "desc" else "ASC"
            total = db.execute("SELECT COUNT(*) FROM entries WHERE parent=?", (path,)).fetchone()[0]
            rows = db.execute(f"SELECT * FROM entries WHERE parent=? ORDER BY {sort_column} {direction}, name COLLATE NOCASE ASC LIMIT ? OFFSET ?",
                              (path, limit, offset)).fetchall()
            return {"path": path, "parent": row["parent"], "items": [_item(r) for r in rows], "total": total}

    def breakdown(self, path: str | None) -> dict:
        with self.lock, closing(self._connect()) as db:
            root = self.status["root"]
            path = os.path.abspath(path) if path else root
            row = db.execute("SELECT size FROM entries WHERE path=? AND kind='folder'", (path,)).fetchone()
            if row is None:
                raise LookupError("Folder is outside the scanned index or was not scanned.")
            children = db.execute("""SELECT name,path,kind,size FROM entries WHERE parent=?
                                    ORDER BY size DESC, name COLLATE NOCASE LIMIT 13""", (path,)).fetchall()
            items = [dict(child) for child in children[:12]]
            other_size = row["size"] - sum(item["size"] for item in items)
            if len(children) > 12:
                items.append({"name": "Other items", "path": None, "kind": "other", "size": other_size})
            return {"path": path, "total_bytes": row["size"], "items": items}

    def files(self, query: str, extension: str, offset: int, limit: int) -> dict:
        with self.lock, closing(self._connect()) as db:
            clauses = ["kind='file'"]
            values: list[object] = []
            if query:
                clauses.append("instr(lower(path), lower(?)) > 0")
                values.append(query)
            if extension:
                clauses.append("extension=?")
                values.append("" if extension == "[none]" else
                              extension.lower() if extension.startswith(".") else "." + extension.lower())
            where = " AND ".join(clauses)
            total = db.execute(f"SELECT COUNT(*) FROM entries WHERE {where}", values).fetchone()[0]
            rows = db.execute(f"SELECT * FROM entries WHERE {where} ORDER BY size DESC, name COLLATE NOCASE LIMIT ? OFFSET ?",
                              [*values, limit, offset]).fetchall()
            return {"items": [_item(r) for r in rows], "total": total}

    def types(self) -> dict:
        with self.lock, closing(self._connect()) as db:
            rows = db.execute("""SELECT extension, SUM(size) AS size, COUNT(*) AS count
                                 FROM entries WHERE kind='file' GROUP BY extension
                                 ORDER BY size DESC, extension""").fetchall()
            return {"items": [dict(row) for row in rows]}

    def errors(self) -> dict:
        with self.lock, closing(self._connect()) as db:
            rows = db.execute("SELECT path,error FROM errors ORDER BY rowid").fetchall()
            return {"items": [dict(row) for row in rows], "total": len(rows)}

    def export_rows(self) -> Iterator[bytes]:
        with self.lock, closing(self._connect()) as db:
            yield b"\xef\xbb\xbf"
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["path", "name", "size", "modified", "extension"])
            yield output.getvalue().encode("utf-8")
            for row in db.execute("SELECT path,name,size,modified,extension FROM entries WHERE kind='file' ORDER BY path"):
                output.seek(0)
                output.truncate(0)
                writer.writerow(tuple(_csv_safe(value) for value in row))
                yield output.getvalue().encode("utf-8")

    def close(self) -> None:
        self.cancel_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        if self.db_path:
            try:
                os.unlink(self.db_path)
            except OSError:
                pass


def _item(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in ("name", "path", "kind", "size", "files", "folders", "modified")}


def _is_reparse_stat(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_link(path: str) -> bool:
    return os.path.islink(path) or _is_reparse_stat(os.stat(path, follow_symlinks=False))


def _csv_safe(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _recycle_folder(path: str) -> None:
    if os.name != "nt":
        raise OSError("The Recycle Bin is available only on Windows.")

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

        def __init__(self, value: str) -> None:
            super().__init__((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

    def method(pointer: ctypes.c_void_p, index: int, *argtypes: type) -> object:
        address = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0][index]
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(address)

    def checked(result: int, action: str) -> None:
        if result < 0:
            raise OSError(f"Recycle Bin {action} failed (HRESULT 0x{result & 0xFFFFFFFF:08X}).")

    ole = ctypes.windll.ole32
    shell = ctypes.windll.shell32
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    checked(ole.CoInitializeEx(None, 2), "initialization")
    operation = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole.CoCreateInstance.restype = ctypes.c_long
        checked(ole.CoCreateInstance(ctypes.byref(GUID("3AD05575-8857-4850-9277-11B85BDB8E09")),
                                     None, 1, ctypes.byref(GUID("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8")),
                                     ctypes.byref(operation)), "creation")
        shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                      ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        shell.SHCreateItemFromParsingName.restype = ctypes.c_long
        checked(shell.SHCreateItemFromParsingName(path, None,
                ctypes.byref(GUID("43826D1E-E718-42EE-BC55-A1E261C37BFE")), ctypes.byref(item)),
                "folder lookup")
        # IFileOperation flags explicitly require recycling and stop on errors.
        flags = 0x00080000 | 0x00100000 | 0x00000400 | 0x00000004
        checked(method(operation, 5, ctypes.c_ulong)(operation, flags), "flag setup")
        checked(method(operation, 18, ctypes.c_void_p, ctypes.c_void_p)(operation, item, None), "queue")
        checked(method(operation, 21)(operation), "operation")
        aborted = ctypes.c_int()
        checked(method(operation, 22, ctypes.POINTER(ctypes.c_int))(operation, ctypes.byref(aborted)),
                "verification")
        if aborted.value or os.path.exists(path):
            raise OSError("Windows did not move the folder to the Recycle Bin.")
    finally:
        if item.value:
            method(item, 2)(item)
        if operation.value:
            method(operation, 2)(operation)
        ole.CoUninitialize()
