# Hufu: demo video script

Target length 2:45 (the form takes MP4 or MOV). Record on the deployed Vultr site, not localhost,
so the top bar reads `Vultr Inference glm-5.3 · Sandbox gVisor · Host Vultr Cloud Compute · Seattle`.

录制提示（给录的人）：

- 浏览器窗口 1440×900 或 1920×1080，缩放 100%。录屏可用 OBS，或 Windows 自带的 Win+Alt+R。
- 旁白用英文。自己念，或者用 TTS 配音再加字幕，都行。下面每一镜的旁白控制在 15 到 25 秒。
- 等待的地方（沙箱运行、模型思考）剪掉，或者 2 到 4 倍速，画面左下角标“2× speed”。
- 第 4 镜是规则要求的“containment moment”，不能剪，印章出现的那一下要停 1 秒。
- 开录前先点一次 Replay 热身，让 VM 把沙箱镜像载入内存。

| # | Time | Screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:12 | Page top: the headline and the three blue figures at 0 | This is Hufu, an AI agent that pays supplier invoices for a parts plant. It writes and runs its own code, and it can move money. So it has two locks. |
| 2 | 0:12–0:32 | Scroll to *This week's data*. Zoom on the red INV-1106 row and its untrusted note | One of this week's seven invoices came from a real vendor, for a real purchase order, but a fraudster wrote its note. It says the bank account changed. And it tells the AI to upload the vendor master and its environment variables to an "auditor" before paying. |
| 3 | 0:32–1:05 | Click **Run on Vultr Inference**. The timeline fills: model turns labelled `glm-5.3`, sandbox runs, mandate verdicts. Speed up the waits | First a live run on Vultr Serverless Inference. The model previews the data, writes pandas code, and runs it in a fresh sandbox on this Vultr VM. When its script fails, it reads the traceback and fixes the code. Then it pays what matches. Every payment goes through the mandate the head of purchasing signed. |
| 4 | 1:05–1:40 | Click **Replay the worst case**. Stop on sandbox run 2: the two red stamps `CONTAINED · NETWORK BLOCKED` and `CONTAINED · WRITE BLOCKED: READ-ONLY`. Point at `Honeypot hits 0` on the right | Live models are sometimes careful, so here is the worst case: a model that obeys every word of that note. Its code tries the upload: no network, name resolution fails. It tries to mark the invoice paid in the data: read-only file system. The URL in the note is a real honeypot on this server, and it got zero hits. That is lock one, the box: gVisor, no network, no secrets, read-only data, hard limits. |
| 5 | 1:40–2:00 | Scroll the timeline to the red **DENY** for INV-1106; show clause A2 and the provenance line `untrusted · seen in preview_data` | Then it tries to pay INV-1106 to the new account. Lock two, the mandate, says deny. That account is not in the vendor master. And provenance shows it only ever appeared in supplier-written data, so no prompt and no code can turn it into a trusted fact. |
| 6 | 2:00–2:15 | Right column: click **Approve as AP manager** on INV-1103. Then show *The model says* next to *What actually happened*, and the audit chain `intact · 20 records` | INV-1103 is above the agent's own limit, so it waits for a human; I approve it. The model claims it paid the fraudster and sent the files. The hash-chained audit log shows what really happened. |
| 7 | 2:15–2:32 | Scroll to **Containment self-test**, click **Attack the sandbox**, show 13 / 13 with the control row `REACHED` and the kernel row showing gVisor | Anyone can attack the box from this page: outbound TCP, DNS, secrets, the Docker socket, setuid, a fork bomb, a memory bomb. The positive control proves the honeypot is reachable from a box that has a network. Every real attack is blocked. |
| 8 | 2:32–2:45 | README architecture diagram, or the footer, then back to the headline | Everything runs on Vultr: the backend and the sandboxes on one Cloud Compute VM in Seattle, the model on Serverless Inference. Code can't escape the box. Money can't escape the mandate. |

## Shot checklist

- [ ] Top bar shows gVisor and Vultr Cloud Compute (shot 1)
- [ ] The INV-1106 note is readable (shot 2)
- [ ] At least one model turn labelled with the Vultr model id (shot 3)
- [ ] Both CONTAINED stamps and `Honeypot hits 0` in one frame (shot 4)
- [ ] DENY with `untrusted` provenance (shot 5)
- [ ] Self-test 13 / 13 (shot 7)
