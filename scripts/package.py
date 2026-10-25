"""Create a source-only deployment archive. Explicit allowlist excludes local state."""
from pathlib import Path
import hashlib
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'dist'
FILES=('install.sh','requirements.txt','VERSION','README.md','LICENSE','THIRD_PARTY_NOTICES.md')
FOLDERS=('app','config','deploy','docs','tests','scripts','browser-extension')


def main():
    OUT.mkdir(exist_ok=True)
    target=OUT/'one-click-proxy-sub.tar.gz'
    with tarfile.open(target,'w:gz',format=tarfile.PAX_FORMAT) as archive:
        candidates=[ROOT/name for name in FILES]
        for folder in FOLDERS:
            candidates.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and not {'__pycache__','_metadata'}.intersection(p.parts) and p.suffix not in ('.pyc','.pyo'))
        for path in sorted(candidates):
            info=archive.gettarinfo(str(path),arcname=path.relative_to(ROOT).as_posix())
            info.uid=info.gid=0;info.uname=info.gname='root';info.mode=0o755 if path.name=='install.sh' else 0o644
            with path.open('rb') as source:archive.addfile(info,source)
    extension=OUT/'browser-extension.zip'
    with zipfile.ZipFile(extension,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT/'browser-extension').rglob('*')):
            if path.is_file() and '_metadata' not in path.parts:
                archive.write(path,path.relative_to(ROOT).as_posix())
    sums=[]
    for path in (target,extension):
        checksum=hashlib.sha256(path.read_bytes()).hexdigest()
        sums.append(checksum+'  '+path.name+'\n')
        print(path)
        print('SHA256 '+checksum)
    (OUT/'SHA256SUMS').write_text(''.join(sums),encoding='ascii')


if __name__=='__main__':main()
