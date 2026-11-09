"""Dump the methods/properties of a COM server's type library WITHOUT starting it.

usage: python typelib_dump.py <ProgID> [name-filter-regex]
e.g.   python typelib_dump.py BlazeRouter.Application
       python typelib_dump.py PowerPCB.Application "Route|Trace|Via|Rule"
"""
import re, sys, winreg
import pythoncom

progid = sys.argv[1]
flt = re.compile(sys.argv[2], re.I) if len(sys.argv) > 2 else None

def reg(path, name=""):
    # PADS 9.x is 32-bit: its CLSIDs live in the WOW64 32-bit registry view
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path, 0,
                                winreg.KEY_READ | view) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            pass
    raise FileNotFoundError(path)

if progid.lower().endswith((".exe", ".tlb", ".dll", ".olb")):
    tlb_path = progid                       # a file: load its (embedded) typelib
else:
    clsid = reg(progid + r"\CLSID")
    try:
        server = reg(rf"CLSID\{clsid}\LocalServer32")
    except OSError:
        server = reg(rf"CLSID\{clsid}\InprocServer32")
    try:
        tlb_id = reg(rf"CLSID\{clsid}\TypeLib")
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"TypeLib\{tlb_id}") as k:
            ver = winreg.EnumKey(k, 0)
        tlb_path = reg(rf"TypeLib\{tlb_id}\{ver}\0\win32")
    except OSError:                         # no TypeLib key: use the server exe
        tlb_path = re.sub(r'\s+/\w+.*$', "", server).strip('"')
    print(f"{progid}: clsid={clsid}\n  server={server}\n  typelib={tlb_path}\n")

tlb = pythoncom.LoadTypeLib(tlb_path)
KIND = {pythoncom.TKIND_DISPATCH: "dispatch", pythoncom.TKIND_INTERFACE: "interface",
        pythoncom.TKIND_ENUM: "enum", pythoncom.TKIND_COCLASS: "coclass"}
INV = {1: "", 2: "get ", 4: "put ", 8: "putref "}
for i in range(tlb.GetTypeInfoCount()):
    ti = tlb.GetTypeInfo(i)
    name = tlb.GetDocumentation(i)[0]
    ta = ti.GetTypeAttr()
    kind = KIND.get(ta.typekind)
    if kind is None or kind == "coclass":
        continue
    lines = []
    if kind == "enum":
        for v in range(ta.cVars):
            vd = ti.GetVarDesc(v)
            lines.append(f"    {ti.GetNames(vd.memid)[0]} = {vd.value}")
    else:
        for f in range(ta.cFuncs):
            fd = ti.GetFuncDesc(f)
            names = ti.GetNames(fd.memid)
            if names[0] in ("QueryInterface", "AddRef", "Release", "GetTypeInfoCount",
                            "GetTypeInfo", "GetIDsOfNames", "Invoke"):
                continue
            lines.append(f"    {INV.get(fd.invkind, '?')}{names[0]}({', '.join(names[1:])})")
    if flt:
        lines = [l for l in lines if flt.search(l) or flt.search(name)]
    if lines or not flt:
        print(f"[{kind}] {name}")
        print("\n".join(lines))
