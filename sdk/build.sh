#!/usr/bin/env bash
set -euo pipefail
out=$(realpath -m "${1:?usage: sdk/build.sh <outdir>}")
here=$(cd "$(dirname "$0")" && pwd)
version=0.2.0
export SOURCE_DATE_EPOCH=1792195200
mkdir -p "$out"
work=$(mktemp -d "${TMPDIR:-/tmp}/stuckbot-sdk-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
echo "== python: build wheel and sdist"
python3 -m venv "$work/build"
"$work/build/bin/pip" install -q --disable-pip-version-check setuptools==84.0.0 build==1.6.1
mkdir "$work/src"
cp -r "$here/python/pyproject.toml" "$here/python/README.md" "$here/python/LICENSE" "$here/python/src" "$work/src/"
find "$work/src" -name __pycache__ -prune -exec rm -rf {} +
"$work/build/bin/python" -m build -q --no-isolation --sdist --wheel --outdir "$out" "$work/src"
whl="$out/stuckbot-$version-py3-none-any.whl"
sdist="$out/stuckbot-$version.tar.gz"
test -f "$whl"
test -f "$sdist"
"$work/build/bin/python" -I - "$sdist" <<'PY'
import gzip,io,os,sys,tarfile
path=sys.argv[1]
epoch=int(os.environ["SOURCE_DATE_EPOCH"])
buf=io.BytesIO()
with tarfile.open(path) as src,tarfile.open(fileobj=buf,mode="w",format=tarfile.PAX_FORMAT) as out:
    for m in sorted(src.getmembers(),key=lambda m:m.name):
        data=src.extractfile(m) if m.isfile() else None
        m.mtime=epoch
        m.uid=m.gid=0
        m.uname=m.gname=""
        m.mode=0o755 if m.isdir() else 0o644
        m.pax_headers={}
        out.addfile(m,data)
with open(path,"wb") as raw,gzip.GzipFile(filename="",mode="wb",fileobj=raw,mtime=0) as gz:
    gz.write(buf.getvalue())
PY
smoke_py='import stuckbot,os
assert stuckbot.__version__=="'"$version"'"
assert os.path.exists(os.path.join(os.path.dirname(stuckbot.__file__),"py.typed"))
c=stuckbot.Client(token="sba_x",base_url="http://127.0.0.1:9")
assert len(stuckbot.OPERATIONS)>=29 and callable(c.create_ticket) and callable(stuckbot.FleetAgent)
print("python smoke ok:",stuckbot.__file__)'
echo "== python: install the wheel into a clean venv"
python3 -m venv "$work/wheel"
"$work/wheel/bin/pip" install -q --disable-pip-version-check --no-index "$whl"
(cd "$work" && "$work/wheel/bin/python" -I -c "$smoke_py")
echo "== python: install the sdist into a clean venv"
python3 -m venv "$work/sdist"
"$work/sdist/bin/pip" install -q --disable-pip-version-check "setuptools>=77"
"$work/sdist/bin/pip" install -q --disable-pip-version-check --no-index --no-build-isolation "$sdist"
(cd "$work" && "$work/sdist/bin/python" -I -c "$smoke_py")
echo "== js: npm pack"
tgz=$(cd "$here/js" && npm pack --silent --pack-destination "$out")
tgz="$out/$tgz"
test "$tgz" = "$out/stuckbot-$version.tgz"
echo "== js: install the tarball into a clean project"
mkdir "$work/npm"
(cd "$work/npm" && npm init -y >/dev/null && npm install --silent --no-audit --no-fund "$tgz")
test -f "$work/npm/node_modules/stuckbot/index.d.ts"
(cd "$work/npm" && node --input-type=module -e '
import {Client,FleetAgent,OPERATIONS,StuckBotError,VERSION} from "stuckbot";
if(VERSION!=="'"$version"'"||Object.keys(OPERATIONS).length<29||typeof new Client({token:"x"}).createTicket!=="function"||typeof FleetAgent!=="function"||!(new StuckBotError(409,"x","y") instanceof Error)){
throw new Error("js smoke failed");
}
console.log("js smoke ok:",import.meta.resolve("stuckbot"));
')
echo "== artifacts"
(cd "$out" && sha256sum "stuckbot-$version-py3-none-any.whl" "stuckbot-$version.tar.gz" "stuckbot-$version.tgz")
