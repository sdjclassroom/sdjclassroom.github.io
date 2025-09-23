#!/usr/bin/env python3
"""
make_single_bundle.py

Creates a single-file HTML bundle for a repo (default: sdjclassroom/sdjclassroom.github.io).
- downloads/clones the repo (if needed),
- finds files to embed (roms, cores, assets, and index.html),
- base64-encodes them and injects a JS map into index.html,
- overrides window.fetch() in the page to serve embedded files from memory,
- writes bundle.html.

Usage:
    python make_single_bundle.py --github sdjclassroom/sdjclassroom.github.io
    python make_single_bundle.py --local /path/to/sdjclassroom.github.io

Notes:
- Requires Python 3.9+
- If git is available the script will try to `git clone` the repo; otherwise it downloads the zip archive.
"""
import argparse
import base64
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests

# ----------------------
# Configuration
# ----------------------
DEFAULT_REPO = "sdjclassroom/sdjclassroom.github.io"
EMBED_PATHS = [ "roms", "assets", "cores", "wasm", "data", "" ]  # directories to embed ("" means root)
INCLUDE_EXTENSIONS = {'.nes', '.sfc', '.smc', '.sna', '.bin', '.zip', '.gba', '.gb', '.gbc',
                      '.iso', '.wasm', '.js', '.css', '.html', '.png', '.jpg', '.jpeg', '.ogg',
                      '.mp3', '.json', '.txt'}
# fallback mime
DEFAULT_MIME = "application/octet-stream"

# ----------------------
# Helpers
# ----------------------
def run_cmd(cmd, cwd=None):
    try:
        subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

def download_repo_zip(owner_repo, dest_dir):
    """Download GitHub zipball for a public repo."""
    url = f"https://github.com/{owner_repo}/archive/refs/heads/main.zip"
    r = requests.get(url, stream=True)
    if r.status_code != 200:
        # try default branch fallback
        url = f"https://github.com/{owner_repo}/archive/refs/heads/master.zip"
        r = requests.get(url, stream=True)
        r.raise_for_status()
    tmpzip = Path(dest_dir) / "repo.zip"
    with open(tmpzip, "wb") as f:
        for chunk in r.iter_content(32768):
            f.write(chunk)
    # extract
    shutil.unpack_archive(str(tmpzip), dest_dir)
    # the zip usually contains a top-level folder like repo-main
    # find it
    for p in Path(dest_dir).iterdir():
        if p.is_dir():
            return str(p)
    return dest_dir

def clone_or_download(owner_repo, dest_dir):
    # try git clone first
    try:
        if run_cmd(["git", "clone", f"https://github.com/{owner_repo}.git", dest_dir]):
            return dest_dir
    except Exception:
        pass
    # fallback to zip download
    print("git clone failed or git not available; falling back to zip download...")
    path = download_repo_zip(owner_repo, dest_dir)
    return path

def collect_files(repo_root: Path):
    """Collect files to embed. Returns dict mapping relative_path -> (mime, b64)"""
    files = {}
    repo_root = Path(repo_root)
    # Walk selected subpaths if they exist, otherwise walk root
    candidate_paths = []
    for p in EMBED_PATHS:
        pw = repo_root / p
        if pw.exists() and pw.is_dir():
            candidate_paths.append(pw)
    if not candidate_paths:
        candidate_paths = [repo_root]
    for base in candidate_paths:
        for root, _, filenames in os.walk(base):
            for fname in filenames:
                fpath = Path(root) / fname
                rel = fpath.relative_to(repo_root).as_posix()
                ext = fpath.suffix.lower()
                if ext not in INCLUDE_EXTENSIONS:
                    # include index.html and any referenced HTML/JS/CSS anyway
                    if rel == "index.html" or ext in {'.html', '.css', '.js'}:
                        pass
                    else:
                        continue
                try:
                    data = fpath.read_bytes()
                except Exception as e:
                    print(f"warning reading {fpath}: {e}")
                    continue
                b64 = base64.b64encode(data).decode('ascii')
                mime = mimetypes.guess_type(fpath.name)[0] or DEFAULT_MIME
                files[rel] = {"mime": mime, "b64": b64}
    # Always include index.html if present
    idx = repo_root / "index.html"
    if idx.exists():
        data = idx.read_text(encoding="utf-8")
        # We'll embed the original text separately as well if it wasn't collected as bytes
        if "index.html" not in files:
            files["index.html"] = {"mime": "text/html", "b64": base64.b64encode(data.encode("utf-8")).decode("ascii")}
    return files

# ----------------------
# JS fetch override to inject into index.html
# ----------------------
FETCH_OVERRIDE_JS = r"""
<script>
/*
  Embedded-files fetch interception.
  window.__EMBEDDED_FILES should be an object:
    { "path/relative/to/repo": { mime: "...", b64: "base64..." }, ... }
  This patch intercepts window.fetch() and serves embedded files when requested.
*/
(function(){
  if (!window.__EMBEDDED_FILES) return;
  const EMB = window.__EMBEDDED_FILES;

  // helper to make Response from base64
  function base64ToResponse(b64, mime) {
    const binary = atob(b64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i=0;i<len;i++) bytes[i]=binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: mime });
    return new Response(blob, { headers: { "Content-Type": mime }});
  }

  const origFetch = window.fetch.bind(window);
  window.fetch = function(resource, init){
    try {
      // resource may be Request or string
      let url;
      if (typeof resource === "string") url = resource;
      else if (resource && resource.url) url = resource.url;
      else url = String(resource);

      // normalize to path relative to origin, drop query and hash
      const u = new URL(url, location);
      let path = u.pathname.replace(/^\/+/, ''); // remove leading slash
      // some pages request same-origin absolute paths, others relative: try both
      if (EMB[path]) {
        return Promise.resolve(base64ToResponse(EMB[path].b64, EMB[path].mime));
      }
      // also try matching just the filename (for some emulator fetches)
      const filename = path.split('/').pop();
      if (filename && EMB[filename]) {
        return Promise.resolve(base64ToResponse(EMB[filename].b64, EMB[filename].mime));
      }
    } catch (e) {
      // fallthrough to network
      console.warn("embedded fetch interception error:", e);
    }
    return origFetch(resource, init);
  };

  // Also intercept XHR open/send for older code that uses XHR
  (function(){
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      this._url = url;
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
      try {
        const u = new URL(this._url, location);
        let path = u.pathname.replace(/^\/+/, '');
        if (EMB[path]) {
          const resp = base64ToResponse(EMB[path].b64, EMB[path].mime);
          resp.arrayBuffer().then(buf => {
            // simulate async XHR ready states
            this.readyState = 4;
            this.status = 200;
            this.response = buf;
            this.responseText = new TextDecoder().decode(buf);
            if (typeof this.onreadystatechange === 'function') this.onreadystatechange();
            if (typeof this.onload === 'function') this.onload();
          });
          return;
        }
      } catch(e){}
      return origSend.apply(this, arguments);
    };
  })();
})();
</script>
"""

# ----------------------
# Main bundling logic
# ----------------------
def bundle_repo_to_single_html(source_dir, output_file="bundle.html", inject_fetch_override=True):
    repo_root = Path(source_dir)
    if not repo_root.exists():
        raise FileNotFoundError(source_dir)
    files = collect_files(repo_root)

    # read index.html content (prefer repo's index.html)
    if "index.html" in files:
        # decode base64 to text
        idx_html = base64.b64decode(files["index.html"]["b64"]).decode("utf-8", errors="replace")
    else:
        # find any html file in root
        idx_path = repo_root / "index.html"
        if idx_path.exists():
            idx_html = idx_path.read_text(encoding="utf-8")
        else:
            # fallback: create a minimal shell that references emulator assets
            idx_html = "<!doctype html><html><head><meta charset='utf-8'><title>Bundle</title></head><body><h1>Bundle</h1></body></html>"

    # Build the JS object literal for embedded files
    # To keep the bundle size reasonable in memory, we will emit a compact JSON map, where values include mime and b64
    EMBED_OBJ = {}
    for rel, info in files.items():
        # Keep paths normalized (no leading slash)
        key = rel.lstrip('/')
        EMBED_OBJ[key] = {"mime": info["mime"], "b64": info["b64"]}

    # JSON stringify (no pretty) but be careful w/ size
    embed_json = json.dumps(EMBED_OBJ, separators=(',',':'))

    # prepare injection script
    injection = f"\n<script>window.__EMBEDDED_FILES = {embed_json};</script>\n"
    if inject_fetch_override:
        injection += FETCH_OVERRIDE_JS + "\n"

    # Insert injection into index.html: try before </head> else before </body> else at end
    out_html = idx_html
    if "</head>" in out_html:
        out_html = out_html.replace("</head>", injection + "</head>", 1)
    elif "</body>" in out_html:
        out_html = out_html.replace("</body>", injection + "</body>", 1)
    else:
        out_html = out_html + injection

    Path(output_file).write_text(out_html, encoding="utf-8")
    print(f"Wrote bundled single-file HTML to: {output_file}")
    return output_file

def main():
    parser = argparse.ArgumentParser(description="Make single-file bundle for a GitHub Pages emulator repo.")
    parser.add_argument("--github", help="owner/repo (e.g. sdjclassroom/sdjclassroom.github.io)", default=DEFAULT_REPO)
    parser.add_argument("--local", help="use already-cloned repo at this path instead of downloading")
    parser.add_argument("--out", help="output file name", default="bundle.html")
    args = parser.parse_args()

    if args.local:
        repo_path = args.local
        if not Path(repo_path).exists():
            print("local path does not exist:", repo_path)
            sys.exit(1)
    else:
        tmp = tempfile.TemporaryDirectory()
        print("cloning/downloading repo into temporary dir...")
        repo_path = clone_or_download(args.github, tmp.name)
        print("repo available at:", repo_path)

    try:
        bundle_repo_to_single_html(repo_path, output_file=args.out)
        print("Done.")
    finally:
        # if we used tmp, it will be cleaned up on program exit (TemporaryDirectory does that)
        pass

if __name__ == "__main__":
    main()
