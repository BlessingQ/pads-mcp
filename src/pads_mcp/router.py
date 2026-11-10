"""PADS Router (BlazeRouter.Application) via COM: design-rule check only (routing is done in PADS Layout).

The router always works on a COPY of the saved .pcb opened without a file lock, so PADS Layout keeps
the original. Router COM cannot set rules; rules come from the design (layout_set_rules).
Must run on the COM worker thread.
"""
from __future__ import annotations

import glob
import os
import shutil
import time

import pythoncom
import win32com.client.dynamic

from .logic import getp, items

MM = 4                     # blazeUnit metric
NET = 2                    # blazeObjectTypeNet
ERROR_CLASS = {0: "clearance", 1: "fabrication", 2: "testability", 3: "connectivity", 4: "length",
               5: "diffpair", 6: "pcb", 7: "max_via_count"}


class PadsRouter:
    PROGID = "BlazeRouter.Application"

    def __init__(self, workdir: str):
        self.tmpdir = os.path.join(workdir, "tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._app = None

    def app(self):
        if self._app is not None:
            try:
                _ = self._app.Name
                return self._app
            except Exception:  # noqa: BLE001
                self._app = None
        try:
            unk = pythoncom.GetActiveObject(self.PROGID)
            a = win32com.client.dynamic.Dispatch(unk.QueryInterface(pythoncom.IID_IDispatch))
        except pythoncom.com_error:
            try:
                a = win32com.client.dynamic.Dispatch(self.PROGID)
            except pythoncom.com_error:
                time.sleep(5)              # e.g. a previous router instance was still shutting down
                a = win32com.client.dynamic.Dispatch(self.PROGID)
        a.Visible = True
        self._app = a
        return a

    def _open_copy(self, path: str, tag: str):
        for old in glob.glob(os.path.join(self.tmpdir, f"router_{tag}_*.pcb")):
            try:
                os.remove(old)
            except OSError:
                pass                                    # still open in the router
        copy = os.path.join(self.tmpdir, f"router_{tag}_{time.strftime('%H%M%S')}.pcb")
        shutil.copy2(path, copy)
        a = self.app()
        cur = a.ActiveDocument
        if cur is not None:
            try:
                cur.Saved = True          # a modified scratch copy would silently block the next open
            except pythoncom.com_error:
                pass
        a.OpenDocumentNoLock(copy)
        d = a.ActiveDocument
        if d is None or os.path.normcase(d.FullName) != os.path.normcase(copy):
            raise RuntimeError(f"PADS Router did not open {copy}")
        return a, d, copy

    # ------------------------------------------------------------------ DRC
    def drc(self, path: str, max_errors: int = 80, timeout: float = 300) -> dict:
        a, d, copy = self._open_copy(path, "drc")
        c = d.Checker
        t = time.time()
        c.Run = True
        while not c.CheckerComplete:
            if time.time() - t > timeout:
                c.Run = False
                raise TimeoutError(f"DRC did not finish within {timeout:.0f}s")
            pythoncom.PumpWaitingMessages()
            time.sleep(0.3)
        errs = d.Errors
        by_class: dict[str, int] = {}
        rows = []
        for e in items(errs):
            cls = ERROR_CLASS.get(e.ErrorClass, str(e.ErrorClass))
            by_class[cls] = by_class.get(cls, 0) + 1
            if len(rows) >= max_errors:
                continue
            row = {"class": cls, "description": e.Description, "layer": e.LayerNumber,
                   "x": round(getp(e, "PositionX", MM), 4), "y": round(getp(e, "PositionY", MM), 4)}
            try:
                if e.HasActualValue:
                    row["actual"] = round(getp(e, "ActualValue", MM), 4)
                if e.HasRequiredValues:
                    row["required_min"] = round(getp(e, "RequiredValueMin", MM), 4)
                    row["required_max"] = round(getp(e, "RequiredValueMax", MM), 4)
            except pythoncom.com_error:
                pass
            try:
                row["objects"] = [x.ConflictObjectDesc for x in items(e.Conflicts)][:4]
            except pythoncom.com_error:
                pass
            rows.append(row)
        return {"unit": "mm", "checked": copy, "seconds": round(time.time() - t, 1),
                "error_count": errs.Count, "by_class": by_class, "unrouted_connections": d.UnrouteCount,
                "errors": rows, "truncated": errs.Count > len(rows)}
