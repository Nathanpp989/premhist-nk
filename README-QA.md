# PremHist QA: install and run guide

This package installs **PremHist** (the historian, its web viewer and PremCalc) on a Windows 11 PC for
QA testing, together with everything that feeds it live data and the **Describe** feature that drafts
calcs and displays from plain English. You don't need Python, Rust, admin rights or any coding: the
package brings its own Python, and any Python already on the PC is neither used nor changed.

What runs when you start it:

| Part | What it does | Address (default) |
|---|---|---|
| Historian + viewer | Stores the data and serves the viewer | http://127.0.0.1:4050 (gRPC 4051) |
| OPC UA simulator | A simulated plant, *PremPlant*, with 88 tags | opc.tcp://127.0.0.1:4840 |
| Buffer agent | Queues data on disk and forwards it to the historian | 4053 (health http://127.0.0.1:4054/) |
| OPC UA collector | Reads the simulator and writes through the agent (dataset **PremPlantUA**) | health http://127.0.0.1:4055/ |
| MCP adapter | The Historian MCP v1.1 interface for AI tools | http://127.0.0.1:4052/mcp |
| Describe service | Drafts calcs and displays with your local Ollama model | http://127.0.0.1:4060 |

Everything stays on this computer. The only network calls go to Ollama, and by default that's local too.

---

## 1. Before you install

You need two things:

1. **Windows 11** (or Windows 10), 64-bit.
2. **Ollama** from ollama.com, running, with at least one model pulled. Open a Command Prompt and run:

   ```
   ollama pull gpt-oss:20b
   ```

   **About models:** Describe was tuned on **gpt-oss:20b**, which needs about **16 GB of GPU memory**.
   Smaller models work, but they draft noticeably worse. That's expected and is **not a bug**, so
   please don't report a weak draft from a small model as one. Note which model you used in every
   Describe report.

## 2. Install

1. **Unblock the zip before you extract it.** Right-click the zip, choose **Properties**, tick
   **Unblock** and click OK. This saves you a Windows warning on every file.
2. Extract the zip anywhere, for example `Downloads`.
3. Open the extracted folder and double-click **Setup.cmd**.
   - If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. The
     package isn't signed, which is normal for a QA build.
4. Setup checks each prerequisite. If something is missing, it says so in plain words with the exact
   fix, and changes nothing until it's fixed.
5. Setup shows the **model menu**: the models you have pulled, each with its size and whether it is
   likely to fit your GPU. Type a number, or press **Enter** to keep the current model.

Setup installs into `%LOCALAPPDATA%\PremHist-QA` and creates these shortcuts:

- **Start menu > PremHist QA**: *Start PremHist*, *Stop PremHist*, *Open PremHist*,
  *Switch Describe model*, *Show logs* and *Uninstall PremHist QA*.
- **Desktop**: *Start PremHist* and *Open PremHist*.

Running Setup again is safe. It updates the program and keeps your data and your model choice. To
install a new build, do the same thing: unzip the new package and run its Setup.cmd. If PremHist is
running, Setup stops it first.

## 3. Start, use, stop

- **Start PremHist** starts every part in order and checks each one is healthy before starting the
  next. It then prints a status table with every part, its state and the Describe model in use, and
  opens the viewer. A later start takes about 10-20 seconds. The **first** start takes longer -
  usually one to two minutes - because it also reads the simulator's 88 tags and adds the example
  plant, and it waits for the first tag values before seeding.
- In the viewer, the **PremPlantUA** tags fill with live data within a few seconds.
- **Stop PremHist** stops exactly the parts that Start PremHist started, and stops the historian
  cleanly. Your data stays.
- **Open PremHist** opens the viewer, http://127.0.0.1:4050.

**If the viewer doesn't open.** The historian listens on `127.0.0.1` only. On some PCs the name
`localhost` resolves to the IPv6 address `::1` first, or goes through a system proxy with no
loopback bypass, and then `http://localhost:4050` cannot connect even though PremHist is running
and the status table says `healthy`. Use **http://127.0.0.1:4050** instead — Start PremHist checks
this and prints the address that works. If neither opens, see section 10.

After a reboot, just run **Start PremHist** again. Nothing starts automatically.

**Examples to start from.** Start PremHist also adds a small example plant (each start adds only
what's missing, so your own changes are kept):

- **An asset hierarchy** (the **Assets** side panel): *Pump Station* with ten pumps, *Tank Farm*
  with 30 tanks and *Electrical* with four feeders, on three templates. Pumps P-505 to P-510 have no
  vibration sensor, and *Retired Meter* points at a deleted tag, on purpose.
- **Four displays** (the **Displays** tab): *Pump Station Overview* (click a pump to open its
  faceplate), *Pump Faceplate* (switch pumps with the element picker), *Tank Farm* and *Electrical
  Feeders*.
- **Four calcs** (the **Calcs** tab): *Flow Deviation*, *Bearing Temp 15m Average* and *Tank Farm
  Average Level* are approved and write a value every minute; *Pump Daily Run Hours* is left as a
  **draft** so you can try the dry run and approval yourself.

If you archive or retire an example, it stays that way. To get the full set back, uninstall with
data deleted and set up again.

From a PowerShell window, the same scripts take options (they live in `%LOCALAPPDATA%\PremHist-QA`):

```
.\start-premhist-qa.ps1 -Model qwen3:8b    # use this model for Describe (saved)
.\start-premhist-qa.ps1 -NoDescribe        # without the Describe service
.\start-premhist-qa.ps1 -Pump              # also run the gRPC data pump (a second live dataset, PremPlant)
.\stop-premhist-qa.ps1
```

If PowerShell refuses to run a script, start it like this:
`powershell -ExecutionPolicy Bypass -File .\start-premhist-qa.ps1`

## 4. Switch the Describe model

Use **Start menu > PremHist QA > Switch Describe model**. It shows the same menu as Setup, saves your
choice and restarts **only** the Describe service. Nothing else restarts, and the next Describe uses
the new model. The status table and the two Describe logs (`logs\calc_describe_log.jsonl` and
`logs\display_describe_log.jsonl`) all show which model was used.

If you pull a new model while PremHist is running, run Switch Describe model to pick it.

## 5. If Ollama isn't running

Everything except Describe keeps working. Describe tells you plainly that the model isn't reachable.
Start Ollama, and Describe works again without restarting PremHist.

## 6. Logs and where things live

Everything is under `%LOCALAPPDATA%\PremHist-QA`. **Show logs** opens the `logs` folder.

| Path | Contents |
|---|---|
| `logs\<part>.log`, `logs\<part>.err.log` | The output of each part for the current run (`server`, `sim`, `mcp`, `agent`, `collector`, `describe`, `pump`). The previous run is kept as `*.prev.log`. |
| `logs\calc_describe_log.jsonl` | One line per calc Describe: your request, the outcome, the model and how long it took. |
| `logs\display_describe_log.jsonl` | The same, one line per display Describe. `outcome` is `draft`, `edit`, `no_change` (Describe deliberately drew nothing), `refusal`, `bad_request` or `error`; `symbols` and `glyphs` say what was drawn, and `missing` lists the words in your request that nothing in the historian matched. The drawn document itself is not logged — only the counts. |
| `logs\collector-browse.err.log` | The first start's tag discovery from the simulator. |
| `logs\seed.log`, `logs\seed.err.log` | Adding the example hierarchy, displays and calcs on each start. |
| `logs\seedlate.log`, `logs\seedlate.err.log` | The pass that binds simulator tags which reported late. |
| `data\` | The historian archive, the agent queue, the collector state and the simulator status. |
| `qa-config.json` | Ports, Ollama address and Describe model. |
| `run\pids.json` | The processes Start PremHist started. Stop uses only these. |

Both Describe logs rotate: at 8 MB the file becomes `*.prev.jsonl` and a fresh one starts, so at most two generations exist per engine and zipping `logs` for a bug report stays practical. Set `PREMALYTICS_DESCRIBE_LOG_MAX_MB` to change the size, or to `off` to keep the whole history.

To read those logs rather than squint at them, run **`tools\describe-report.cmd`**. It prints how many Describes ran, the mix of outcomes, median and p95 seconds, anything that went over the drafting budget, the words in your requests that nothing in the plant matched, and every refusal and error with its reason. `--since 2026-11-01` narrows the window, `--csv report` writes a spreadsheet, `--json` gives machine-readable output. It reads only, and needs no running PremHist.

If Describe is misbehaving and you want one answer rather than a hunt, POST to **`/api/describe/selftest`** (or just run `tools\diagnose-describe.ps1`, which now calls it for you). It reports whether both engines import, whether this process can reach PremHist, what each engine's grounding found for a canned request, the plant index cache, and whether the model you picked is actually pulled in Ollama. It makes no model call and writes nothing, so it takes a second rather than a minute.

`GET /health` also now reports the version, how long the service has been up, and the plant index cache - including whether the index was **warmed at startup**. The warm is what stops the first calc Describe after a restart from reading the whole plant before it begins; the start script configures it, and `/health` names the user it warmed for, because the cache is per user.

## 7. Reporting a bug

Include:

1. what you did, what you expected and what happened, with a screenshot if it's visual;
2. the **version**, the first line of `VERSION.txt`;
3. the **Describe model**, for anything about Describe;
4. a zip of the **`logs`** folder. Don't zip `data`, which can be large, unless asked.

## 8. Uninstall

Use **Start menu > PremHist QA > Uninstall PremHist QA**, or run `Setup.cmd -Uninstall` from the
package. It stops PremHist, removes the shortcuts and asks before deleting your data. Type `YES` to
delete the data; press Enter to keep it.

## 9. Known limits of this QA build

- **Unsigned.** Windows warns once (see section 2). This is normal for QA.
- **One PremHist QA per PC** on the default ports. If something else uses ports 4050-4055, 4060 or
  4840 (another PremHist, for example), install with other ports: `Setup.cmd -BasePort 5050` (gives
  5050-5055, Describe 5060, OPC UA 5840). After changing ports on an existing install, delete
  `data\agent` before starting, because the agent remembers the old historian address.
- **The OPC UA chain uses security None** (local simulator only).
- **Describe quality depends on the model** (see section 1). Drafting can take a minute or more on a
  smaller GPU.
- **Local only.** The viewer is at `127.0.0.1`; other PCs can't reach it. The name `localhost` is
  not used, because it does not reach a service bound to the IPv4 loopback on every PC (see
  section 3).
- **Ollama on another PC:** `Setup.cmd -OllamaUrl http://<host>:11434`.

## 10. The viewer will not open

Run these five in a PowerShell window; they tell apart the causes. Use `curl.exe`, not `curl` (in
PowerShell `curl` is an alias for something else).

```powershell
# 1. Is anything listening on 4050, and on which address?
Get-NetTCPConnection -State Listen -LocalPort 4050 |
    Select-Object LocalAddress, LocalPort, OwningProcess

# 2. Does the IPv4 loopback answer? (this is what Start PremHist health-checks)
curl.exe -sS -i http://127.0.0.1:4050/api/v1/health

# 3. Does the name answer?
curl.exe -sS -i http://localhost:4050/api/v1/health

# 4. How does the name resolve, and is a proxy in the way?
Resolve-DnsName localhost | Select-Object Name, Type, IPAddress
netsh winhttp show proxy
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' |
    Select-Object ProxyEnable, ProxyServer, ProxyOverride

# 5. What does PremHist think is running, and did the historian say anything?
Get-Content "$env:LOCALAPPDATA\PremHist-QA\run\pids.json"
Get-Content "$env:LOCALAPPDATA\PremHist-QA\logs\server.err.log" -Tail 30
```

| What you see | What it is | What to do |
|---|---|---|
| 2 answers, 3 does not | `localhost` does not reach the IPv4 loopback on this PC (`::1` first, or a proxy with no loopback bypass) | Use `http://127.0.0.1:4050`. Add `localhost;127.0.0.1` to the proxy bypass list if there is a proxy. |
| 1 lists no listener | The historian is not running, whatever the status table said | Read `server.err.log` (step 5), then Stop PremHist and start again |
| 1 lists a listener owned by an unexpected program | Something else holds 4050 | Stop it, or reinstall on other ports: `Setup.cmd -BasePort 5050` |
| 2 and 3 both fail but 1 lists `ph-server` | Security software or a proxy is intercepting loopback | Exempt `ph-server.exe` and the loopback addresses |
| Start PremHist said "already running" | Parts from an earlier run are still alive | Stop PremHist, then start again |

Include the output of all five when reporting it (section 7).
