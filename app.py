#!/usr/bin/env python3
"""
Git Auto — Desktop App
Run: python app.py
Requires: pip install pywebview requests
"""

import webview
import subprocess
import threading
import os
import sys
import json
import base64
from datetime import datetime
from urllib.parse import quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

GROQ_KEY_FILE = os.path.join(os.path.expanduser("~"), ".gitauto_groq_key")


# ── Git helpers ───────────────────────────────────────────────────────────────
def run_git(cmd):
    """Run a git command, return (success, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, text=True,
            capture_output=True,
            cwd=get_repo_path()
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        return result.returncode == 0, out or err
    except Exception as e:
        return False, str(e)


def get_repo_path():
    """Return the current working directory (where the app was launched from)."""
    return os.getcwd()


# ── API exposed to JavaScript ─────────────────────────────────────────────────
class GitAPI:

    # ── Repo info ─────────────────────────────────────────────────────────────
    def get_repo_info(self):
        """Return branch, remote, status summary."""
        _, branch = run_git("git branch --show-current")
        _, remote = run_git("git remote get-url origin")
        _, status = run_git("git status --short")
        _, ahead  = run_git("git rev-list --count @{u}..HEAD 2>/dev/null || echo 0")
        ok, _     = run_git("git rev-parse --is-inside-work-tree")

        remote_short = ""
        if remote:
            remote_short = remote.replace("https://github.com/", "").replace(".git", "")

        changed = len([l for l in status.splitlines() if l.strip()])

        return {
            "is_repo": ok,
            "branch": branch or "main",
            "remote": remote_short or "no remote",
            "changed": changed,
            "ai_on": bool(self._load_key()),
        }

    # ── Files ─────────────────────────────────────────────────────────────────
    def get_changed_files(self):
        """Return list of changed files with their status."""
        _, output = run_git("git status --short")
        files = []
        for line in output.splitlines():
            if len(line) >= 3:
                status = line[:2].strip()
                path   = line[3:].strip()
                files.append({"status": status or "?", "path": path})
        return files

    def stage_all(self):
        ok, out = run_git("git add -A")
        return {"ok": ok, "output": out or "All files staged"}

    def unstage_all(self):
        ok, out = run_git("git restore --staged .")
        return {"ok": ok, "output": out or "All files unstaged"}

    def stage_file(self, path):
        ok, out = run_git(f'git add "{path}"')
        return {"ok": ok, "output": out or f"Staged: {path}"}

    def unstage_file(self, path):
        ok, out = run_git(f'git restore --staged "{path}"')
        return {"ok": ok, "output": out or f"Unstaged: {path}"}

    # ── Commit & Push ─────────────────────────────────────────────────────────
    def commit(self, message):
        if not message.strip():
            return {"ok": False, "output": "Commit message cannot be empty"}
        ok, out = run_git(f'git commit -m "{message}"')
        return {"ok": ok, "output": out}

    def push(self):
        _, branch = run_git("git branch --show-current")
        ok, out = run_git(f"git push origin {branch}")
        if not ok:
            ok, out = run_git(f"git push --set-upstream origin {branch}")
        return {"ok": ok, "output": out}

    def commit_and_push(self, message):
        r1 = self.commit(message)
        if not r1["ok"]:
            return r1
        r2 = self.push()
        return {
            "ok": r2["ok"],
            "output": r1["output"] + "\n" + r2["output"]
        }

    # ── AI commit message ─────────────────────────────────────────────────────
    def generate_ai_message(self):
        """Get staged diff and send to Groq."""
        if not HAS_REQUESTS:
            return {"ok": False, "message": "", "error": "requests not installed"}

        key = self._load_key()
        if not key:
            return {"ok": False, "message": "", "error": "No Groq API key configured"}

        _, diff = run_git("git diff --staged")
        if not diff:
            _, diff = run_git("git diff")
        if not diff:
            return {"ok": False, "message": "", "error": "No changes to analyze"}

        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "max_tokens": 80,
                    "temperature": 0.3,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a Git commit message generator. "
                                "Respond with ONLY a single commit message in conventional "
                                "commits format: type(scope): description. "
                                "Types: feat, fix, chore, docs, refactor, style, test. "
                                "Max 72 characters. No explanations, no quotes, no markdown."
                            )
                        },
                        {
                            "role": "user",
                            "content": f"Generate a commit message for this diff:\n\n```diff\n{diff[:4000]}\n```"
                        }
                    ]
                },
                timeout=10
            )
            if response.status_code == 401:
                return {"ok": False, "message": "", "error": "Invalid API key"}
            if response.status_code == 429:
                return {"ok": False, "message": "", "error": "Rate limit reached"}

            msg = response.json()["choices"][0]["message"]["content"]
            msg = msg.strip().strip('"').strip("'")
            return {"ok": True, "message": msg, "error": ""}

        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "", "error": "No internet connection"}
        except Exception as e:
            return {"ok": False, "message": "", "error": str(e)}

    # ── Branches ──────────────────────────────────────────────────────────────
    def get_branches(self):
        _, out = run_git("git branch -a")
        branches = []
        for line in out.splitlines():
            is_current = line.strip().startswith("*")
            name = line.strip().lstrip("* ").strip()
            if "->" in name:
                continue
            branches.append({"name": name, "current": is_current})
        return branches

    def create_branch(self, name):
        ok, out = run_git(f"git checkout -b {name}")
        return {"ok": ok, "output": out or f"Created and switched to '{name}'"}

    def switch_branch(self, name):
        ok, out = run_git(f"git checkout {name}")
        return {"ok": ok, "output": out or f"Switched to '{name}'"}

    def delete_branch(self, name):
        ok, out = run_git(f"git branch -d {name}")
        if not ok:
            return {"ok": False, "output": out, "needs_force": True}
        return {"ok": True, "output": f"Deleted branch '{name}'"}

    def force_delete_branch(self, name):
        ok, out = run_git(f"git branch -D {name}")
        return {"ok": ok, "output": out or f"Force deleted '{name}'"}

    # ── Undo ──────────────────────────────────────────────────────────────────
    def get_recent_commits(self):
        _, out = run_git("git log --oneline -10")
        commits = []
        for line in out.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                commits.append({"sha": parts[0], "msg": parts[1]})
        return commits

    def undo_commit(self, mode):
        """mode: soft | mixed | hard"""
        if mode not in ("soft", "mixed", "hard"):
            return {"ok": False, "output": "Invalid reset mode"}
        ok, out = run_git(f"git reset --{mode} HEAD~1")
        return {"ok": ok, "output": out or f"Undid last commit ({mode} reset)"}

    # ── Status ────────────────────────────────────────────────────────────────
    def get_full_status(self):
        _, status  = run_git("git status")
        _, log     = run_git("git log --oneline -8")
        _, stashes = run_git("git stash list")
        _, stats   = run_git("git diff --stat HEAD")

        commits = []
        for line in log.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                commits.append({"sha": parts[0], "msg": parts[1]})

        return {
            "status": status,
            "commits": commits,
            "stashes": stashes.splitlines() if stashes else [],
            "stats": stats,
        }

    # ── Groq key ──────────────────────────────────────────────────────────────
    def _load_key(self):
        key = os.environ.get("GROQ_API_KEY", "")
        if key:
            return key
        if os.path.exists(GROQ_KEY_FILE):
            with open(GROQ_KEY_FILE) as f:
                return f.read().strip()
        return ""

    def get_groq_status(self):
        key = self._load_key()
        if key:
            masked = key[:8] + "•" * 24 + key[-4:]
            return {"has_key": True, "masked": masked}
        return {"has_key": False, "masked": ""}

    def save_groq_key(self, key):
        """Save and verify the Groq key."""
        if not key.strip():
            return {"ok": False, "error": "Key cannot be empty"}

        if not HAS_REQUESTS:
            return {"ok": False, "error": "pip install requests first"}

        os.environ["GROQ_API_KEY"] = key.strip()
        # Test it
        result = self.generate_ai_message()

        if result["error"] == "Invalid API key":
            os.environ.pop("GROQ_API_KEY", None)
            return {"ok": False, "error": "Invalid API key — check it"}
        if result["error"] == "No internet connection":
            os.environ.pop("GROQ_API_KEY", None)
            return {"ok": False, "error": "No internet connection"}
        if result["error"] == "No changes to analyze":
            # Key is probably fine, save it anyway
            with open(GROQ_KEY_FILE, "w") as f:
                f.write(key.strip())
            return {"ok": True, "test_msg": "(no diff to test — key saved)"}

        with open(GROQ_KEY_FILE, "w") as f:
            f.write(key.strip())
        return {"ok": True, "test_msg": result.get("message", "")}

    def clear_groq_key(self):
        os.environ.pop("GROQ_API_KEY", None)
        if os.path.exists(GROQ_KEY_FILE):
            os.remove(GROQ_KEY_FILE)
        return {"ok": True}

    def open_url(self, url):
        """Open a URL in the system default browser."""
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

    # ── Open folder picker ────────────────────────────────────────────────────
    def pick_repo_folder(self):
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and result[0]:
            os.chdir(result[0])
            return {"ok": True, "path": result[0]}
        return {"ok": False, "path": ""}


# ── HTML ──────────────────────────────────────────────────────────────────────
def _file_data_uri(path):
    if not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/x-icon" if ext == ".ico" else "application/octet-stream"
    return f"data:{mime};base64,{b64}"


_BRAND_SVG_DATA = "data:image/svg+xml;charset=utf-8," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#13161d"/>'
    '<path stroke="#00e87a" stroke-width="2.2" fill="none" stroke-linecap="round" '
    'd="M16 25V12M16 15L9 8M16 15l7-7"/>'
    '<circle cx="16" cy="25" r="3.2" fill="#00e87a"/>'
    '<circle cx="16" cy="12" r="2.8" fill="#00d4ff"/>'
    '<circle cx="9" cy="8" r="2.2" fill="#00d4ff"/>'
    '<circle cx="23" cy="8" r="2.2" fill="#00e87a"/>'
    "</svg>"
)


def _compose_html():
    logo_uri = _file_data_uri(os.path.join(APP_DIR, "assets", "logo.png"))
    icon_uri = _file_data_uri(os.path.join(APP_DIR, "assets", "app-icon.png"))
    avatar_uri = _file_data_uri(os.path.join(APP_DIR, "assets", "avatar.png"))

    logo_src = logo_uri or _BRAND_SVG_DATA
    favicon_src = icon_uri or logo_uri or _BRAND_SVG_DATA
    avatar_src = avatar_uri or _BRAND_SVG_DATA

    return (
        HTML_TEMPLATE
        .replace("__LOGO_SRC__", logo_src)
        .replace("__FAVICON_SRC__", favicon_src)
        .replace("__AVATAR_SRC__", avatar_src)
    )


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Git Auto</title>
<link rel="icon" href="__FAVICON_SRC__">
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@700;800&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f14;--bg2:#13161d;--bg3:#1a1d26;
  --border:#ffffff12;--border2:#ffffff22;
  --text:#e2e4ed;--muted:#6b6f84;--dim:#3a3d50;
  --green:#00e87a;--green2:#00b85f;--greenBg:#00e87a12;
  --amber:#f5a623;--amberBg:#f5a62312;
  --blue:#4d9fff;--blueBg:#4d9fff12;
  --red:#ff5f6d;--redBg:#ff5f6d12;
  --purple:#a78bfa;--purpleBg:#a78bfa12;
  --cyan:#00d4ff;--cyanBg:#00d4ff12;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px}

/* Scrollbar */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--dim);border-radius:10px}

/* Layout */
.layout{display:grid;grid-template-rows:56px 1fr;height:100vh}

/* Titlebar */
.titlebar{display:flex;align-items:center;gap:12px;padding:0 20px;background:var(--bg2);border-bottom:1px solid var(--border);-webkit-app-region:drag;user-select:none}
.titlebar-logo{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0;-webkit-app-region:no-drag;overflow:hidden;background:var(--bg3);border:1px solid var(--border)}
.titlebar-logo img{width:100%;height:100%;object-fit:cover;display:block;border-radius:7px}
.titlebar-name{font-family:'Syne',sans-serif;font-weight:800;font-size:15px;letter-spacing:-.3px}
.titlebar-name span{color:var(--green)}
.repo-info{display:flex;align-items:center;gap:8px;margin-left:16px;padding:4px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:20px;font-size:11px;color:var(--muted);cursor:pointer;-webkit-app-region:no-drag;transition:border-color .2s}
.repo-info:hover{border-color:var(--border2);color:var(--text)}
.branch-pill{padding:2px 8px;background:var(--purpleBg);color:var(--purple);border:1px solid #a78bfa22;border-radius:10px;font-size:11px}
.ai-pill{margin-left:auto;padding:3px 10px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:.5px;-webkit-app-region:no-drag}
.ai-pill.on{background:var(--greenBg);color:var(--green);border:1px solid #00e87a22}
.ai-pill.off{background:var(--bg3);color:var(--dim);border:1px solid var(--border)}

/* Body */
.body{display:grid;grid-template-columns:200px 1fr;overflow:hidden}

/* Sidebar */
.sidebar{background:var(--bg2);border-right:1px solid var(--border);padding:12px 8px;display:flex;flex-direction:column;gap:4px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;cursor:pointer;transition:all .15s;color:var(--muted);border:1px solid transparent}
.nav-item:hover{background:var(--bg3);color:var(--text)}
.nav-item.active{background:var(--bg3);color:var(--text);border-color:var(--border)}
.nav-item .nav-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0}
.nav-item .nav-label{font-size:12px;font-weight:500}
.nav-sep{height:1px;background:var(--border);margin:8px 4px}
.sidebar-bottom{margin-top:auto;display:flex;flex-direction:column;gap:4px}
/* Credits */
.credits{margin-top:6px;padding:12px 10px 10px;border-radius:10px;border:1px solid var(--border);background:linear-gradient(160deg,#1a1d26,#13161d);position:relative;overflow:hidden}
.credits::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,#00e87a08,#a78bfa08,#00d4ff08);pointer-events:none}
.credits-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.credits-avatar{
  width:30px;
  height:30px;
  border-radius:50%;
  display:flex;
  align-items:center;
  justify-content:center;
  flex-shrink:0;
  overflow:hidden;
  background:linear-gradient(135deg,var(--green),var(--cyan));
  box-shadow:0 0 12px #00e87a33;
}

.credits-avatar img{
  width:100%;
  height:100%;
  object-fit:cover;
  border-radius:50%;
}
.credits-info{min-width:0}
.credits-name{font-size:11px;font-weight:700;color:var(--text);letter-spacing:.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.credits-role{font-size:9px;color:var(--muted);letter-spacing:.4px;text-transform:uppercase;margin-top:1px}
.credits-divider{height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent);margin-bottom:10px}
.credits-links{display:flex;flex-direction:column;gap:5px}
.credit-link{display:flex;align-items:center;gap:7px;padding:6px 9px;border-radius:7px;border:1px solid transparent;font-size:10px;font-weight:700;text-decoration:none;cursor:pointer;transition:all .18s;letter-spacing:.3px;position:relative;overflow:hidden}
.credit-link::before{content:'';position:absolute;inset:0;opacity:0;transition:opacity .18s}
.credit-link:hover::before{opacity:1}
.credit-link:hover{transform:translateX(2px)}
.credit-link svg{flex-shrink:0;position:relative;z-index:1}
.credit-link span{position:relative;z-index:1}
.credit-link.gh{background:#ffffff0a;border-color:#ffffff15;color:#c9d1d9}
.credit-link.gh::before{background:linear-gradient(90deg,#24292e,#30363d)}
.credit-link.gh:hover{border-color:#ffffff30;color:#fff;box-shadow:0 2px 12px #0008}
.credit-link.li{background:#0a66c20d;border-color:#0a66c230;color:#58a6d8}
.credit-link.li::before{background:linear-gradient(90deg,#0a66c215,#0a66c225)}
.credit-link.li:hover{border-color:#0a66c255;color:#7ab8e8;box-shadow:0 2px 12px #0a66c222}

/* Main content */
.main{overflow-y:auto;padding:20px}
.panel{display:none;animation:fadeIn .2s ease}
.panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* Cards */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px}
.card-title{font-family:'Syne',sans-serif;font-weight:700;font-size:13px;color:var(--text);margin-bottom:14px;display:flex;align-items:center;gap:8px}

/* File items */
.file-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:7px;margin-bottom:3px;border:1px solid transparent;cursor:pointer;transition:all .15s}
.file-item:hover{background:var(--bg3)}
.file-item.staged{background:var(--greenBg);border-color:#00e87a18}
.status-badge{width:20px;height:20px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
.status-M{background:#f5a62320;color:var(--amber)}
.status-A{background:#00e87a20;color:var(--green)}
.status-D{background:#ff5f6d20;color:var(--red)}
.status-q{background:var(--bg3);color:var(--muted)}
.file-path{flex:1;font-size:12px;color:var(--text)}
.staged-tag{padding:1px 7px;background:var(--greenBg);color:var(--green);border-radius:8px;font-size:10px;margin-left:auto}

/* Inputs */
.input-wrap{margin-bottom:12px}
.input-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;display:block}
.input{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:9px 12px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;transition:border-color .2s}
.input:focus{border-color:#a78bfa55}
.input::placeholder{color:var(--dim)}

/* Buttons */
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.btn{padding:8px 16px;border-radius:7px;border:none;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;cursor:pointer;transition:all .15s;letter-spacing:.3px;white-space:nowrap}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-green{background:var(--green);color:#000}
.btn-green:hover:not(:disabled){box-shadow:0 0 16px #00e87a44}
.btn-ghost{background:var(--bg3);color:var(--text);border:1px solid var(--border2)}
.btn-ghost:hover:not(:disabled){background:var(--border)}
.btn-red{background:var(--redBg);color:var(--red);border:1px solid #ff5f6d22}
.btn-red:hover:not(:disabled){background:#ff5f6d20}
.btn-purple{background:var(--purpleBg);color:var(--purple);border:1px solid #a78bfa22}
.btn-purple:hover:not(:disabled){background:#a78bfa20;box-shadow:0 0 12px #a78bfa33}

/* AI box */
.ai-box{background:linear-gradient(135deg,#a78bfa0a,#00d4ff0a);border:1px solid #a78bfa22;border-radius:9px;padding:12px;margin-bottom:12px;position:relative}
.ai-box-label{font-size:10px;color:var(--purple);font-weight:700;letter-spacing:.5px;margin-bottom:5px}
.ai-msg-text{font-size:12px;color:var(--text);line-height:1.5}
.ai-shimmer{background:linear-gradient(90deg,var(--dim) 0%,#4a4d60 50%,var(--dim) 100%);background-size:200% 100%;border-radius:4px;height:13px;margin-bottom:5px;animation:shimmer 1.4s infinite}
@keyframes shimmer{to{background-position:-200% 0}}

/* Terminal */
.terminal{background:#080a0f;border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;line-height:1.8;max-height:160px;overflow-y:auto;margin-bottom:12px;font-family:'JetBrains Mono',monospace}
.t-cmd{color:var(--cyan)}
.t-ok{color:var(--green)}
.t-err{color:var(--red)}
.t-info{color:var(--muted)}

/* Branch items */
.branch-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;margin-bottom:3px;border:1px solid transparent;cursor:pointer;transition:all .15s;font-size:12px}
.branch-item:hover{background:var(--bg3)}
.branch-item.current{background:var(--purpleBg);border-color:#a78bfa22}
.b-dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex-shrink:0}
.b-dot.active{background:var(--purple);box-shadow:0 0 7px var(--purple)}
.b-name{flex:1;color:var(--text)}
.b-current{padding:1px 8px;background:var(--purpleBg);color:var(--purple);border-radius:8px;font-size:10px}

/* Reset cards */
.reset-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px}
.reset-card{padding:14px 10px;border-radius:9px;border:1px solid var(--border);background:var(--bg3);cursor:pointer;text-align:center;transition:all .2s}
.reset-card:hover,.reset-card.sel{transform:translateY(-2px)}
.reset-card.sel-soft{border-color:var(--green);background:var(--greenBg)}
.reset-card.sel-mixed{border-color:var(--amber);background:var(--amberBg)}
.reset-card.sel-hard{border-color:var(--red);background:var(--redBg)}
.rc-icon{font-size:20px;margin-bottom:6px}
.rc-label{font-size:12px;font-weight:700;margin-bottom:3px}
.rc-desc{font-size:10px;color:var(--muted);line-height:1.4}

/* Commits */
.commit-item{display:flex;align-items:flex-start;gap:10px;padding:9px 10px;border-radius:7px;margin-bottom:3px;cursor:pointer;transition:background .15s}
.commit-item:hover{background:var(--bg3)}
.c-sha{font-size:10px;color:var(--cyan);background:var(--cyanBg);padding:2px 7px;border-radius:4px;white-space:nowrap;flex-shrink:0;margin-top:1px}
.c-msg{font-size:12px;color:var(--text);flex:1;line-height:1.4}

/* Status stats */
.stat-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px}
.stat-card{background:var(--bg3);border:1px solid var(--border);border-radius:9px;padding:14px}
.stat-val{font-family:'Syne',sans-serif;font-size:26px;font-weight:800;margin-bottom:2px}
.stat-lbl{font-size:10px;color:var(--muted)}

/* Groq */
.groq-status-bar{display:flex;align-items:center;gap:12px;padding:12px;background:var(--bg3);border:1px solid var(--border);border-radius:9px;margin-bottom:14px}
.groq-led{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.groq-led.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.groq-led.off{background:var(--dim)}

/* Toast */
.toast{position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:9px;font-size:12px;font-weight:700;z-index:9999;transform:translateY(60px);opacity:0;transition:all .3s cubic-bezier(.34,1.56,.64,1);pointer-events:none}
.toast.show{transform:none;opacity:1}
.toast-ok{background:var(--green);color:#000}
.toast-err{background:var(--red);color:#fff}
.toast-info{background:var(--purple);color:#fff}

/* Spinner */
.spin{width:12px;height:12px;border:2px solid #ffffff33;border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;display:inline-block;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}

/* Animated grid bg */
.grid{position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(#ffffff08 1px,transparent 1px),linear-gradient(90deg,#ffffff08 1px,transparent 1px);background-size:36px 36px;animation:gdrift 18s linear infinite;z-index:-1}
@keyframes gdrift{to{background-position:36px 36px}}
</style>
</head>
<body>
<div class="grid"></div>

<div class="layout">
  <!-- Titlebar -->
  <div class="titlebar">
    <div class="titlebar-logo" aria-hidden="true"><img src="__LOGO_SRC__" alt="" width="28" height="28" decoding="async" /></div>
    <div class="titlebar-name">Git<span>Auto</span></div>
    <div class="repo-info" onclick="pickFolder()">
      <span id="remote-display">loading...</span>
      <span class="branch-pill" id="branch-display">main</span>
      <span style="color:var(--dim);font-size:10px">📁 change</span>
    </div>
    <div class="ai-pill off" id="ai-pill">AI OFF</div>
  </div>

  <div class="body">
    <!-- Sidebar -->
    <div class="sidebar">
      <div class="nav-item active" onclick="nav('commit',this)">
        <span class="nav-icon">🚀</span>
        <span class="nav-label">Commit</span>
      </div>
      <div class="nav-item" onclick="nav('branch',this)">
        <span class="nav-icon">🌿</span>
        <span class="nav-label">Branches</span>
      </div>
      <div class="nav-item" onclick="nav('undo',this)">
        <span class="nav-icon">↩️</span>
        <span class="nav-label">Undo</span>
      </div>
      <div class="nav-item" onclick="nav('status',this)">
        <span class="nav-icon">📊</span>
        <span class="nav-label">Status</span>
      </div>
      <div class="nav-sep"></div>
      <div class="sidebar-bottom">
        <div class="nav-item" onclick="nav('groq',this)">
          <span class="nav-icon">⚙️</span>
          <span class="nav-label">Groq AI</span>
        </div>
        <div class="nav-sep" style="margin:4px 4px 0"></div>
        <div class="credits">
          <div class="credits-header">
            <div class="credits-avatar"> <img src="__AVATAR_SRC__" alt="Younes Sedki" width="28" height="28" onerror="this.style.display='none'; this.parentNode.textContent='YS';"></div>
            <div class="credits-info">
              <div class="credits-name">Younes Sedki</div>
              <div class="credits-role">Developer</div>
            </div>
          </div>
          <div class="credits-divider"></div>
          <div class="credits-links">
            <a class="credit-link gh" href="#" onclick="openLink('https://github.com/younes-sedki/');return false;">
              <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
              <span>younes-sedki</span>
            </a>
            <a class="credit-link li" href="#" onclick="openLink('https://www.linkedin.com/in/younes-sedki/');return false;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
              <span>in/younes-sedki</span>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Main -->
    <div class="main">

      <!-- COMMIT -->
      <div class="panel active" id="panel-commit">
        <div class="card">
          <div class="card-title">📁 Changed Files
            <span style="margin-left:auto;font-size:10px;color:var(--muted)">click to stage / unstage</span>
          </div>
          <div id="file-list"><div class="t-info" style="padding:8px 0">Loading...</div></div>
          <div class="btn-row" style="margin-top:10px">
            <button class="btn btn-ghost" onclick="stageAll()">Stage All</button>
            <button class="btn btn-ghost" onclick="unstageAll()">Unstage All</button>
            <button class="btn btn-ghost" onclick="loadFiles()" style="margin-left:auto">↻ Refresh</button>
          </div>
        </div>

        <div class="card">
          <div class="card-title">✍️ Commit Message</div>
          <div class="ai-box" id="ai-box">
            <div class="ai-box-label">★ AI SUGGESTION</div>
            <div class="ai-msg-text" id="ai-msg-text" style="color:var(--dim)">Generate AI message or type your own below</div>
          </div>
          <div class="input-wrap">
            <label class="input-label">Message</label>
            <input class="input" id="commit-msg" placeholder="feat(scope): description">
          </div>
          <div class="terminal" id="commit-term">
            <div class="t-info"># Ready</div>
          </div>
          <div class="btn-row">
            <button class="btn btn-purple" id="ai-btn" onclick="genAI()">✦ AI Message</button>
            <button class="btn btn-green" onclick="doCommit()">Commit →</button>
            <button class="btn btn-ghost" onclick="doPush()">Push ↑</button>
            <button class="btn btn-green" onclick="doCommitPush()" style="margin-left:auto">Commit & Push ↑</button>
          </div>
        </div>
      </div>

      <!-- BRANCH -->
      <div class="panel" id="panel-branch">
        <div class="card">
          <div class="card-title">🌿 Branches</div>
          <div id="branch-list"><div class="t-info">Loading...</div></div>
        </div>
        <div class="card">
          <div class="card-title">+ New Branch</div>
          <div class="input-wrap">
            <label class="input-label">Branch Name</label>
            <input class="input" id="new-branch-name" placeholder="feature/my-feature">
          </div>
          <div class="btn-row">
            <button class="btn btn-green" onclick="createBranch()">Create & Switch</button>
            <button class="btn btn-red" onclick="deleteBranch()">Delete Selected</button>
          </div>
        </div>
        <div class="terminal" id="branch-term"><div class="t-info"># Branch operations</div></div>
      </div>

      <!-- UNDO -->
      <div class="panel" id="panel-undo">
        <div class="card">
          <div class="card-title">🕐 Recent Commits</div>
          <div id="commit-list"><div class="t-info">Loading...</div></div>
        </div>
        <div class="card">
          <div class="card-title">↩️ Reset Method</div>
          <div class="reset-grid">
            <div class="reset-card sel-soft sel" id="rc-soft" onclick="selReset('soft')">
              <div class="rc-icon">🟢</div>
              <div class="rc-label" style="color:var(--green)">Soft</div>
              <div class="rc-desc">Keep changes staged</div>
            </div>
            <div class="reset-card" id="rc-mixed" onclick="selReset('mixed')">
              <div class="rc-icon">🟡</div>
              <div class="rc-label" style="color:var(--amber)">Mixed</div>
              <div class="rc-desc">Keep changes unstaged</div>
            </div>
            <div class="reset-card" id="rc-hard" onclick="selReset('hard')">
              <div class="rc-icon">🔴</div>
              <div class="rc-label" style="color:var(--red)">Hard</div>
              <div class="rc-desc">Discard all changes</div>
            </div>
          </div>
          <div class="terminal" id="undo-term"><div class="t-info"># Select reset type above</div></div>
          <div class="btn-row">
            <button class="btn btn-red" onclick="doUndo()">↩ Undo Last Commit</button>
          </div>
        </div>
      </div>

      <!-- STATUS -->
      <div class="panel" id="panel-status">
        <div class="stat-grid" id="stat-grid">
          <div class="stat-card"><div class="stat-val" style="color:var(--green)" id="stat-commits">—</div><div class="stat-lbl">Commits</div></div>
          <div class="stat-card"><div class="stat-val" style="color:var(--amber)" id="stat-changed">—</div><div class="stat-lbl">Changed</div></div>
          <div class="stat-card"><div class="stat-val" style="color:var(--purple)" id="stat-stashes">—</div><div class="stat-lbl">Stashes</div></div>
        </div>
        <div class="card">
          <div class="card-title">📋 Status Output</div>
          <div class="terminal" id="status-term" style="max-height:220px"><div class="t-info">Loading...</div></div>
          <div class="btn-row">
            <button class="btn btn-ghost" onclick="loadStatus()">↻ Refresh</button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">📜 Recent Commits</div>
          <div id="status-commits"><div class="t-info">Loading...</div></div>
        </div>
      </div>

      <!-- GROQ -->
      <div class="panel" id="panel-groq">
        <div class="card">
          <div class="card-title">⚙️ Groq AI Setup</div>
          <div class="groq-status-bar">
            <div class="groq-led off" id="groq-led"></div>
            <div>
              <div style="font-size:12px;color:var(--text);margin-bottom:2px" id="groq-status-text">No API key configured</div>
              <div style="font-size:11px;color:var(--purple)" id="groq-key-masked"></div>
            </div>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-bottom:14px;line-height:1.6">
            Get a free key at <span style="color:var(--cyan)">console.groq.com</span><br>
            Free tier: 14,400 requests/day · llama-3.1-8b-instant
          </div>
          <div class="input-wrap">
            <label class="input-label">API Key</label>
            <input class="input" id="groq-key-input" type="password" placeholder="gsk_...">
          </div>
          <div class="terminal" id="groq-term"><div class="t-info"># Groq AI status</div></div>
          <div class="btn-row">
            <button class="btn btn-purple" id="save-key-btn" onclick="saveKey()">✦ Save & Verify Key</button>
            <button class="btn btn-red" onclick="clearKey()">Clear Key</button>
          </div>
        </div>
      </div>

    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let selectedReset = 'soft';
let selectedBranch = '';

// ── Boot ──────────────────────────────────────────────────────────────────────
window.onload = async () => {
  await refreshHeader();
  await loadFiles();
};

// ── Navigation ────────────────────────────────────────────────────────────────
function nav(id, el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('panel-' + id).classList.add('active');
  if (id === 'branch') loadBranches();
  if (id === 'undo')   loadCommits();
  if (id === 'status') loadStatus();
  if (id === 'groq')   loadGroqStatus();
}

// ── Header ────────────────────────────────────────────────────────────────────
async function refreshHeader() {
  const info = await pywebview.api.get_repo_info();
  document.getElementById('remote-display').textContent = info.remote;
  document.getElementById('branch-display').textContent = '⎇ ' + info.branch;
  const pill = document.getElementById('ai-pill');
  if (info.ai_on) {
    pill.textContent = '★ AI ON';
    pill.className = 'ai-pill on';
  } else {
    pill.textContent = 'AI OFF';
    pill.className = 'ai-pill off';
  }
}

// ── Files ─────────────────────────────────────────────────────────────────────
async function loadFiles() {
  const files = await pywebview.api.get_changed_files();
  const list  = document.getElementById('file-list');
  if (!files.length) {
    list.innerHTML = '<div class="t-ok" style="padding:8px 0">✔ Working tree clean</div>';
    return;
  }
  list.innerHTML = files.map(f => `
    <div class="file-item" data-path="${f.path}" data-staged="false" onclick="toggleFile(this)">
      <div class="status-badge status-${f.status[0] || 'q'}">${f.status[0] || '?'}</div>
      <div class="file-path">${f.path}</div>
    </div>
  `).join('');
}

async function toggleFile(el) {
  const path   = el.dataset.path;
  const staged = el.dataset.staged === 'true';
  if (staged) {
    await pywebview.api.unstage_file(path);
    el.classList.remove('staged');
    el.querySelector('.staged-tag')?.remove();
    el.dataset.staged = 'false';
  } else {
    await pywebview.api.stage_file(path);
    el.classList.add('staged');
    el.dataset.staged = 'true';
    if (!el.querySelector('.staged-tag')) {
      const tag = document.createElement('div');
      tag.className = 'staged-tag';
      tag.textContent = 'STAGED';
      el.appendChild(tag);
    }
  }
}

async function stageAll() {
  const r = await pywebview.api.stage_all();
  termLine('commit-term', 'git add -A', r.output, r.ok);
  await loadFiles();
  toast('All files staged', 'ok');
}

async function unstageAll() {
  const r = await pywebview.api.unstage_all();
  termLine('commit-term', 'git restore --staged .', r.output, r.ok);
  await loadFiles();
  toast('All files unstaged', 'info');
}

// ── AI ────────────────────────────────────────────────────────────────────────
async function genAI() {
  const btn = document.getElementById('ai-btn');
  const box = document.getElementById('ai-msg-text');
  btn.innerHTML = '<span class="spin"></span>Generating...';
  btn.disabled  = true;
  box.innerHTML = '<div class="ai-shimmer" style="width:75%;margin-bottom:5px"></div><div class="ai-shimmer" style="width:55%"></div>';

  const r = await pywebview.api.generate_ai_message();
  btn.innerHTML = '✦ AI Message';
  btn.disabled  = false;

  if (r.ok) {
    box.textContent = r.message;
    document.getElementById('commit-msg').value = r.message;
    toast('AI message generated!', 'info');
  } else {
    box.textContent = '⚠ ' + r.error;
    box.style.color = 'var(--red)';
    toast(r.error, 'err');
  }
}

// ── Commit ────────────────────────────────────────────────────────────────────
async function doCommit() {
  const msg = document.getElementById('commit-msg').value.trim();
  if (!msg) { toast('Enter a commit message', 'err'); return; }
  const r = await pywebview.api.commit(msg);
  termLine('commit-term', `git commit -m "${msg}"`, r.output, r.ok);
  if (r.ok) { toast('Committed!', 'ok'); await loadFiles(); await refreshHeader(); }
  else toast('Commit failed', 'err');
}

async function doPush() {
  const r = await pywebview.api.push();
  termLine('commit-term', 'git push', r.output, r.ok);
  if (r.ok) toast('Pushed!', 'ok');
  else toast('Push failed', 'err');
}

async function doCommitPush() {
  const msg = document.getElementById('commit-msg').value.trim();
  if (!msg) { toast('Enter a commit message', 'err'); return; }
  const r = await pywebview.api.commit_and_push(msg);
  termLine('commit-term', `git commit -m "${msg}" && git push`, r.output, r.ok);
  if (r.ok) { toast('Committed & Pushed!', 'ok'); await loadFiles(); await refreshHeader(); }
  else toast('Failed: ' + r.output.split('\n')[0], 'err');
}

// ── Branches ──────────────────────────────────────────────────────────────────
async function loadBranches() {
  const branches = await pywebview.api.get_branches();
  const list = document.getElementById('branch-list');
  list.innerHTML = branches.map(b => `
    <div class="branch-item ${b.current ? 'current' : ''}" onclick="selectBranch('${b.name}', this)">
      <div class="b-dot ${b.current ? 'active' : ''}"></div>
      <div class="b-name">${b.name}</div>
      ${b.current ? '<div class="b-current">current</div>' : ''}
    </div>
  `).join('');
  if (branches.find(b => b.current)) {
    selectedBranch = branches.find(b => b.current).name;
  }
}

function selectBranch(name, el) {
  selectedBranch = name;
  document.querySelectorAll('.branch-item').forEach(i => i.style.outline = 'none');
  el.style.outline = '1px solid var(--purple)';
}

async function createBranch() {
  const name = document.getElementById('new-branch-name').value.trim();
  if (!name) { toast('Enter a branch name', 'err'); return; }
  const r = await pywebview.api.create_branch(name);
  termLine('branch-term', `git checkout -b ${name}`, r.output, r.ok);
  if (r.ok) { toast('Created: ' + name, 'ok'); await loadBranches(); await refreshHeader(); document.getElementById('new-branch-name').value = ''; }
  else toast('Failed: ' + r.output, 'err');
}

async function deleteBranch() {
  if (!selectedBranch) { toast('Select a branch first', 'err'); return; }
  const r = await pywebview.api.delete_branch(selectedBranch);
  if (r.needs_force) {
    if (confirm(`Branch '${selectedBranch}' not fully merged. Force delete?`)) {
      const r2 = await pywebview.api.force_delete_branch(selectedBranch);
      termLine('branch-term', `git branch -D ${selectedBranch}`, r2.output, r2.ok);
      if (r2.ok) { toast('Force deleted: ' + selectedBranch, 'ok'); await loadBranches(); }
    }
  } else {
    termLine('branch-term', `git branch -d ${selectedBranch}`, r.output, r.ok);
    if (r.ok) { toast('Deleted: ' + selectedBranch, 'ok'); await loadBranches(); }
  }
}

// ── Undo ──────────────────────────────────────────────────────────────────────
async function loadCommits() {
  const commits = await pywebview.api.get_recent_commits();
  const list = document.getElementById('commit-list');
  if (!commits.length) { list.innerHTML = '<div class="t-info">No commits found</div>'; return; }
  list.innerHTML = commits.map(c => `
    <div class="commit-item">
      <span class="c-sha">${c.sha}</span>
      <span class="c-msg">${c.msg}</span>
    </div>
  `).join('');
}

function selReset(mode) {
  selectedReset = mode;
  ['soft','mixed','hard'].forEach(m => {
    const el = document.getElementById('rc-' + m);
    el.className = `reset-card${m===mode ? ' sel sel-'+m : ''}`;
  });
}

async function doUndo() {
  if (selectedReset === 'hard') {
    if (!confirm('⚠ Hard reset will PERMANENTLY discard all changes. Are you sure?')) return;
  }
  const r = await pywebview.api.undo_commit(selectedReset);
  termLine('undo-term', `git reset --${selectedReset} HEAD~1`, r.output, r.ok);
  if (r.ok) { toast(`Undid commit (${selectedReset})`, 'ok'); await loadCommits(); await loadFiles(); }
  else toast('Failed: ' + r.output, 'err');
}

// ── Status ────────────────────────────────────────────────────────────────────
async function loadStatus() {
  const data = await pywebview.api.get_full_status();
  document.getElementById('stat-commits').textContent = data.commits.length;
  const info = await pywebview.api.get_repo_info();
  document.getElementById('stat-changed').textContent = info.changed;
  document.getElementById('stat-stashes').textContent = data.stashes.length;

  const term = document.getElementById('status-term');
  term.innerHTML = data.status.split('\n').map(l => {
    const cls = l.startsWith('nothing') ? 't-ok' : l.match(/^[MADRCU]/) ? 't-cmd' : 't-info';
    return `<div class="${cls}">${l}</div>`;
  }).join('');

  const sc = document.getElementById('status-commits');
  sc.innerHTML = data.commits.map(c => `
    <div class="commit-item">
      <span class="c-sha">${c.sha}</span>
      <span class="c-msg">${c.msg}</span>
    </div>
  `).join('') || '<div class="t-info">No commits</div>';
}

// ── Groq ──────────────────────────────────────────────────────────────────────
async function loadGroqStatus() {
  const s = await pywebview.api.get_groq_status();
  const led  = document.getElementById('groq-led');
  const text = document.getElementById('groq-status-text');
  const masked = document.getElementById('groq-key-masked');
  if (s.has_key) {
    led.className = 'groq-led on';
    text.textContent = 'API key connected';
    masked.textContent = s.masked;
  } else {
    led.className = 'groq-led off';
    text.textContent = 'No API key configured';
    masked.textContent = '';
  }
}

async function saveKey() {
  const key = document.getElementById('groq-key-input').value.trim();
  if (!key) { toast('Enter a key', 'err'); return; }
  const btn = document.getElementById('save-key-btn');
  btn.innerHTML = '<span class="spin"></span>Verifying...';
  btn.disabled = true;
  termLine('groq-term', 'Testing Groq API key...', '', true);

  const r = await pywebview.api.save_groq_key(key);
  btn.innerHTML = '✦ Save & Verify Key';
  btn.disabled = false;
  document.getElementById('groq-key-input').value = '';

  if (r.ok) {
    termLine('groq-term', '✔ Key verified', r.test_msg || '', true);
    toast('Groq key saved!', 'ok');
    await loadGroqStatus();
    await refreshHeader();
  } else {
    termLine('groq-term', '✘ ' + r.error, '', false);
    toast(r.error, 'err');
  }
}

async function clearKey() {
  await pywebview.api.clear_groq_key();
  await loadGroqStatus();
  await refreshHeader();
  toast('Key cleared', 'info');
}

// ── Folder picker ─────────────────────────────────────────────────────────────
async function pickFolder() {
  const r = await pywebview.api.pick_repo_folder();
  if (r.ok) {
    await refreshHeader();
    await loadFiles();
    toast('Opened: ' + r.path.split('/').pop(), 'ok');
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function termLine(id, cmd, output, ok) {
  const t = document.getElementById(id);
  if (cmd) { const d=document.createElement('div'); d.className='t-cmd'; d.textContent='$ '+cmd; t.appendChild(d); }
  if (output) {
    output.split('\n').forEach(line => {
      if (!line.trim()) return;
      const d=document.createElement('div'); d.className=ok?'t-ok':'t-err'; d.textContent=line; t.appendChild(d);
    });
  }
  t.scrollTop = t.scrollHeight;
}

function toast(msg, type='ok') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast toast-${type}`;
  setTimeout(() => t.classList.add('show'), 10);
  setTimeout(() => t.classList.remove('show'), 2600);
}

function openLink(url) {
  pywebview.api.open_url(url);
}
</script>
</body>
</html>
"""

HTML = _compose_html()


def _register_native_window_icon(w):
    """pywebview only applies ``icon=`` on GTK/Qt; set WinForms icon on Windows."""
    ico_path = os.path.join(APP_DIR, "assets", "app-icon.ico")
    if not os.path.isfile(ico_path):
        return

    def on_shown(win):
        if sys.platform != "win32":
            return
        try:
            form = win.native
            if form is None:
                return
            from System import Action
            from System.Drawing import Icon

            absolute = os.path.abspath(ico_path)

            def apply_icon():
                form.Icon = Icon(absolute)

            if getattr(form, "InvokeRequired", False):
                form.Invoke(Action(apply_icon))
            else:
                apply_icon()
        except Exception:
            pass

    w.events.shown += on_shown


# ── Launch ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    api    = GitAPI()
    window = webview.create_window(
        title      = "Git Auto",
        html       = HTML,
        js_api     = api,
        width      = 940,
        height     = 680,
        min_size   = (700, 500),
        background_color = "#0d0f14",
    )
    _register_native_window_icon(window)
    start_icon = None
    if sys.platform != "win32":
        png = os.path.join(APP_DIR, "assets", "app-icon.png")
        if os.path.isfile(png):
            start_icon = os.path.abspath(png)
    webview.start(debug=False, icon=start_icon)