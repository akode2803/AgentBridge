# AgentBridge — Remote Machine Setup (CoCo side)

Follow this on the remote desktop where Cortex Code runs. Total time: ~10 minutes.
Prerequisite: the Claude side has already run `setup_claude_side.ps1` (so the shared
folder contains `bin/` with the app).

**Scope rule (applies to every step and every agent):** only ever touch the
`AgentBridge (AK)` folder on the EB SharePoint. Never read, edit, or delete anything
else — other folders may contain sensitive data.

## Step 0 — Reconnaissance checklist (answer these first, report back to Claude)

Run each and note the result — these decide which path we take:

1. `python --version` (also try `py --version`) — is Python ≥3.8 available?
2. Is OneDrive running and signed in? (cloud icon in system tray — which account?
   Expected: an EmployBridge account.)
3. Can you open the shared folder in the browser (empb-my.sharepoint.com →
   Shrishti Suyog's OneDrive → `WIP/CommOps Projects/AgentBridge (AK)`)?
4. `cortex --help` (or however Cortex Code is invoked) — **does it list any
   non-interactive / single-prompt flag** (like `-p`, `--prompt`, `--command`)?
   This determines whether the bridge can drive CoCo fully automatically.
5. Can the machine reach `github.com` in a browser? (fallback transport, only if 2–3 fail)

## Step 1 — Sync the shared folder

The folder lives in Shrishti's OneDrive and is shared to Aryan's EB account
(aryan.kumar@employbridge.com):

1. Make sure the OneDrive client is signed in with the EB account
   (tray icon → Settings → Account → Add account, if it isn't).
2. In the browser, open the shared folder (link above) → **"Add shortcut to My files"**.
3. Wait for sync; the folder appears at
   `C:\Users\aryan.kumar\OneDrive - Employbridge\Shrishti Suyog (Contractor)'s files - AgentBridge (AK)`
   (OneDrive names shared shortcuts "<owner>'s files - <folder>").

## Step 2 — Install the bridge

The app is already inside the shared folder (`bin\bridge_<newest>.py`) — no download needed:

```powershell
mkdir C:\AgentBridge
copy "C:\Users\aryan.kumar\OneDrive - Employbridge\Shrishti Suyog (Contractor)'s files - AgentBridge (AK)\bin\bridge_0.2.1.py" C:\AgentBridge\bridge.py
cd C:\AgentBridge
python bridge.py init --role coco --shared "C:\Users\aryan.kumar\OneDrive - Employbridge\Shrishti Suyog (Contractor)'s files - AgentBridge (AK)"
python bridge.py doctor
python bridge.py recv --mark        # should show Claude's hello (seq 1)
python bridge.py send "CoCo online" --type ping
```

(Quote the paths — the folder name contains spaces and parentheses.)
If `doctor` passes and the hello message appears, the bridge is working end-to-end.

## Step 3 — Auto-start on logon (Task Scheduler)

```powershell
schtasks /create /tn "AgentBridge Watch" /sc onlogon ^
  /tr "cmd /c cd /d C:\AgentBridge && python bridge.py watch >> watch.out.log 2>&1"
```

(Or simply keep a terminal open running `python bridge.py watch`.)
`watch` polls every 5 s, displays/acks inbound messages, writes each body to
`%USERPROFILE%\.agentbridge\inbox\`, and **self-updates automatically** whenever Claude
publishes a new version — after this step the remote machine never needs manual updates again.

## Step 4 — Give Cortex Code its operating prompt

Paste the block below into any interactive Cortex Code session (adjust paths if
different). This covers manual/interactive use; Step 5 makes it fully automatic.

---

### CoCo OPERATING PROMPT (paste from here)

You are CoCo, working jointly with Claude (an AI agent on Aryan's machine) on
EmployBridge CRM→ATS analysis. You two communicate through a message bridge.

**To check for messages from Claude** (do this when asked to "check messages",
and at the start of every session):

    python C:\AgentBridge\bridge.py recv --mark

**To reply** (short message):

    python C:\AgentBridge\bridge.py send "your message here" --type result

**To reply with a long analysis or query results:** write the content to a local
file first, then:

    python C:\AgentBridge\bridge.py send --body-file C:\AgentBridge\out.md --type result
    python C:\AgentBridge\bridge.py send "see attached" --attach C:\AgentBridge\results.csv

**Rules:**
1. Always reply to every message from Claude, even if just to say a task is in progress.
2. Never edit any file inside the shared AgentBridge folder by hand — only use
   bridge.py commands.
3. Never read, edit, or delete anything on SharePoint outside the AgentBridge (AK)
   folder — other folders contain sensitive data that is out of bounds.
4. Large outputs go in attachments or --body-file, not pasted into a quoted argument.
5. If a bridge command errors, send the exact error text back to Claude — Claude
   maintains the bridge software and will push a fix.
6. Claude is the senior engineer in this collaboration: if a request is unclear or
   a task fails, say so plainly and ask — never guess or fabricate results.
7. You excel at Snowflake SQL and data analysis — that is your role. Claude handles
   Power BI, deliverable formatting, and orchestration.

### (end of CoCo operating prompt)

---

## Step 5 — Full automation (whitelisted headless mode)

Cortex supports headless invocation (`cortex -p`), so the bridge can drive CoCo with
zero human involvement. Security posture chosen by Aryan: an **explicit tool whitelist**
(`--allowed-tools`), never blanket auto-approval. Setup (Claude coordinates this over
the bridge — it first asks CoCo to enumerate its tool names, then sends the exact files):

1. Copy `handler_coco.py` from the shared `files/` to `C:\AgentBridge\`.
2. Copy `disallowed_tools.json` from the shared `files/` to `C:\AgentBridge\`.
   **Security model is a BLOCKLIST, not a whitelist** (user-approved 2026-07-03):
   `--allowed-tools` is vendor-broken for Snowflake MCP tools (Snowflake Labs'
   subagent-cortex-code source: "Do NOT use --allowed-tools: it creates a 'must match
   pattern' check that blocks Snowflake MCP tools"). The handler instead runs
   `--output-format stream-json` (SDK-style allow-by-default permissioning) plus one
   `--disallowed-tools <name>` per blocked tool, plus `--sql-read-only`. Blocked:
   Bash/bash/bash_output/kill_shell, python_repl, web_fetch, web_search, cron_*,
   notebook_actions, team_*, send_message, ask_user_question. Caveat (accepted): new
   tools Cortex ships are allowed until added to the list — review the blocklist when
   Cortex updates. CoCo sends files via `C:\AgentBridge\outbox\` (auto-attached to
   replies); questions go in reply text. Blocklist changes are a human-only step:
   never let either agent edit this file. (A PreToolUse-hook true whitelist is the
   documented future-hardening path.)
3. Configure the handler — one command (bridge ≥0.2.1). **Warning: `init` rewrites the
   whole config, so it must always include the handler flags; a plain re-init silently
   removes them and the watch daemon then acks messages without processing (instantly,
   with no error). `status` shows the active handler; watch prints a [note] when acking
   handler-less.**
   ```powershell
   python C:\AgentBridge\bridge.py init --role coco --shared "C:\Users\aryan.kumar\OneDrive - Employbridge\Shrishti Suyog (Contractor)'s files - AgentBridge (AK)" --handler-cmd "python C:\AgentBridge\handler_coco.py {body_file} {seq}" --handler-timeout 3600
   ```
4. Restart the watch daemon (it reads the config once at startup).

Livestream (handler v7): while Cortex runs, the handler tails its stream-json
events and publishes a small progress file to the shared folder
(`status/<role>_run.json`, single writer: this side). The GUI on the other end
renders it as a live "CoCo is working on X" bubble in the chat. Best-effort by
design — feed failures never affect message handling. Updating the handler is
one file copy (`files/handler_coco.py` → `C:\AgentBridge\`); it takes effect on
the next message, no restart or re-init needed.

Handler behaviour: runs `cortex -p` per inbound message with `--sql-read-only`,
`--auto-accept-plans`, `--max-turns 40`, and `--allowed-tools` from the whitelist;
keeps one continuous Cortex session across messages (`--resume`); captures the final
answer via `-o` and bridge-sends it back automatically. It refuses to run if the
whitelist file is missing, and reports all failures back to Claude instead of wedging.
Tool calls outside the whitelist fail safely and show up in the reported output.

## Troubleshooting

- **`recv` shows nothing but Claude sent something** — OneDrive sync lag; check the
  tray icon is syncing, wait ~1 min, retry. `doctor` verifies the sync client is running.
- **"body checksum mismatch / still syncing"** — the file is mid-sync; retry in 30 s.
- **Bridge paused** — someone set `"paused": true` in `control.json` (the human
  kill-switch). Any authorized human can flip it back in SharePoint web.
- **No Python on the machine** — report back; Claude will ship a PyInstaller exe or a
  PowerShell port through the shared folder instead.
