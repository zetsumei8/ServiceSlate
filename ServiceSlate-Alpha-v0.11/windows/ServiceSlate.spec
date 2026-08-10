# PyInstaller standalone Windows build.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
hidden = collect_submodules('serviceslate')
datas = collect_data_files('serviceslate')

a = Analysis(
    [str(root / 'run_serviceslate.py')],
    pathex=[str(root / 'src')],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ServiceSlate', console=False, icon=str(root / 'windows' / 'ServiceSlate.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name='ServiceSlate')
