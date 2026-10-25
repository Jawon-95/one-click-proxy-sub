"""Create a source-only deployment archive. Explicit allowlist excludes local state."""
from pathlib import Path
import hashlib
import tarfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'dist'
FILES=('install.sh','requirements.txt','VERSION','README.md','LICENSE','THIRD_PARTY_NOTICES.md')
FOLDERS=('app','config','deploy','docs','tests','scripts')


def main():
    OUT.mkdir(exist_ok=True)
    target=OUT/'one-click-proxy-sub.tar.gz'
    with tarfile.open(target,'w:gz',format=tarfile.PAX_FORMAT) as archive:
        candidates=[ROOT/name for name in FILES]
        for folder in FOLDERS:
            candidates.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo'))
        for path in sorted(candidates):
            info=archive.gettarinfo(str(path),arcname=path.relative_to(ROOT).as_posix())
            info.uid=info.gid=0;info.uname=info.gname='root';info.mode=0o755 if path.name=='install.sh' else 0o644
            with path.open('rb') as source:archive.addfile(info,source)
    checksum=hashlib.sha256(target.read_bytes()).hexdigest()
    (OUT/'SHA256SUMS').write_text(checksum+'  '+target.name+'\n',encoding='ascii')
    print(target)
    print('SHA256 '+checksum)


if __name__=='__main__':main()
