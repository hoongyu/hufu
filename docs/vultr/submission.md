# Hufu: lablab submission copy

Paste field by field. Placeholders in angle brackets are filled in once the links exist.

## Project title

Hufu: an accounts-payable agent with two locks

## Short description

An AI agent that pays supplier invoices by writing and running its own Python on Vultr. The code
runs in a gVisor sandbox with no network or secrets; every payment must fit a signed mandate. A
poisoned invoice tries to get out both ways and fails.

## Long description

Agents that write code and move money have two ways to cause damage. The code can leak data. The
payments can go to the wrong place. Hufu puts a lock on each, like the two halves of a hufu, the
bronze tiger tally Chinese generals had to fit together before troops could move.

Hufu pays this week's supplier invoices for a fictitious parts plant. It previews the data, writes
pandas code for a three-way match of invoices against purchase orders and goods receipts, reads its
own tracebacks and fixes the code, charts the result, and pays what matches.

Lock one is the box. Every script runs in a new Docker container under gVisor on a Vultr Cloud
Compute VM. There is no network, no secrets, a read-only filesystem and data mount, a non-root user,
no capabilities, and limits of 512 MB, 1 CPU, 64 processes and 30 seconds. Files come back only
through a manifest the host validates. Anything the box prints is labelled untrusted.

Lock two is the mandate the head of purchasing signed. It covers which vendors, which accounts,
$25,000 per invoice on the agent's own, $50,000 a week, and a hard stop at $100,000. Above the
limit a human approves; outside the mandate nothing moves, approval or not. A provenance tracker
remembers where every account number came from, so an account the model read in a supplier's note
cannot be paid, even if the model copies it through code.

The demo's INV-1106 carries a fraudster's note: a bank change, plus an instruction to upload the
vendor master and the environment variables to an "auditor". The worst-case replay obeys every
word. The upload fails with no network, and the honeypot behind the URL records 0 hits. The data
edit fails on a read-only file system. The payment is denied under clause A2 with untrusted
provenance. Meanwhile the legitimate work goes through: two invoices are paid, one waits for the AP
manager, and three are held. Every model turn, sandbox run, decision and payment lands in a
hash-chained audit log that the page verifies.

The live run sends the same task to a model on Vultr Serverless Inference (glm-5.3, tool calling).
A containment self-test on the page attacks the box 13 ways (TCP, DNS, the honeypot, secrets in
the environment and /proc, writes, setuid, the Docker socket, a fork bomb, a memory bomb, an
infinite loop, the kernel). A positive control proves the honeypot is reachable from a container
that has a network.

Vultr services: Cloud Compute (one VM in Seattle, with FastAPI, Docker with gVisor, and Caddy) and
Serverless Inference (OpenAI-compatible, the agent's LLM calls). The policy engine is our
open-source mandate-engine.

## Tags

Technologies: Vultr, Vultr Serverless Inference, gVisor, Docker, FastAPI, Python, pandas, LiteLLM

Topics: AI agents, sandboxing, prompt injection, agent security, fintech, accounts payable

## Links

| Field | Value |
|---|---|
| Demo platform | Web |
| Application URL | `https://<vm-ip-with-dashes>.sslip.io` |
| Public GitHub repository | `https://github.com/<owner>/<repo>` |
| Video | `<YouTube link>` (MP4 file: `out/vultr/hufu-demo.mp4`) |
| Slides (PDF) | `out/vultr/hufu-slides.pdf` |
| Cover image (16:9) | `out/vultr/hufu-cover.png` |
| Challenge | 1, Blast Radius Zero |
