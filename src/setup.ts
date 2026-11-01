// 共享设置 —— 对应 Python 版的 common.py。每个示例都调用 openAgent(),拿到 harness 和它的 root 会话。
import { mkdirSync, readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall, type FauxResponseFactory } from "@earendil-works/pi-ai/providers/faux";
import { openrouterProvider } from "@earendil-works/pi-ai/providers/openrouter";
import { createRegistry, Harness } from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";

export const ctx = BACKGROUND_CONTEXT; // a Chord context that never cancels
// 注意:Windows 上不能直接用 URL.pathname(会得到 "/D:/..." 这样的路径),必须走 fileURLToPath
export const DATA_DIR = fileURLToPath(new URL("../.data/", import.meta.url));
export const DB_FILE = `${DATA_DIR}agent.sqlite`; // 与 Python 版共用同一份 .data 与 .env

loadDotEnv();
const PROVIDER = process.env.PROVIDER === "openrouter" ? "openrouter" : "faux";

/** 保存在会话 pi.agent 文档里的模型选择。 */
export const model =
  PROVIDER === "openrouter"
    ? { provider: "openrouter", modelId: process.env.MODEL ?? "anthropic/claude-sonnet-5.5" }
    : { provider: "faux", modelId: "echo" };

/**
 * Windows 上,pi-durable 的 bash 工具只会在 %ProgramFiles%\Git 和 PATH 里找 bash;
 * Git 装在别处时(如 D:\software\Git),从这里推出 <Git 安装根>\bin\bash.exe,
 * 否则工具会报 "No bash shell found"。也可用环境变量 GIT_BASH 显式指定。
 */
async function resolveBashPath(): Promise<string | undefined> {
  if (process.platform !== "win32") return undefined;
  if (process.env.GIT_BASH && existsSync(process.env.GIT_BASH)) return process.env.GIT_BASH;
  try {
    const { stdout } = await execFileAsync("where", ["git"]);
    const line = stdout.split(/\r?\n/).find(Boolean);
    if (!line) return undefined;
    const gitRoot = dirname(dirname(line.trim())); // git.exe 在 <Git 安装根>\cmd\ 下
    const candidate = join(gitRoot, "bin", "bash.exe");
    return existsSync(candidate) ? candidate : undefined;
  } catch {
    return undefined;
  }
}

/** 打开(或重新打开)同一个 SQLite 文件。同一个文件就是同一个会话。 */
export async function openAgent() {
  mkdirSync(DATA_DIR, { recursive: true });
  const bashPath = await resolveBashPath();
  const models = createModels();
  if (PROVIDER === "openrouter") models.setProvider(openrouterProvider());
  else installFaux(models);

  const registry = createRegistry();
  registry.install(CodingTools); // read, write, edit, bash(Python 版只移植了其中的 bash)

  const harness = await Harness.open(
    await openNodeSqliteStorage(DB_FILE),
    {
      models,
      registry,
      env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? DATA_DIR, shellPath: bashPath }),
    },
    ctx,
  );
  const root = await harness.root(ctx, { agent: { model, cwd: DATA_DIR } });
  return { harness, root };
}

/** 打印会话里所有已存储的条目:编号、种类和一段短文本。 */
export async function printTranscript(root: Awaited<ReturnType<typeof openAgent>>["root"]) {
  const page = await root.entries({}, 200, undefined, ctx);
  for (const e of [...page.items].reverse()) {
    const msg = e.model?.[0] as { role?: string; content?: unknown } | undefined;
    console.log(`  #${e.id}  ${e.kind.padEnd(15)} ${textOf(msg?.content).slice(0, 90)}`);
  }
}

export function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((b: { type: string; text?: string; name?: string; arguments?: unknown }) =>
      b.type === "text" ? b.text ?? "" : b.type === "toolCall" ? `[tool ${b.name} ${JSON.stringify(b.arguments)}]` : "",
    )
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

// 免费、确定性的假模型。文本以 "!" 开头就运行一次 bash,其余原样复读。
function installFaux(models: ReturnType<typeof createModels>) {
  const handle = fauxProvider({
    provider: "faux",
    models: [{ id: "echo", name: "Faux Echo", contextWindow: 128000, maxTokens: 8192 }],
    tokensPerSecond: 400,
  });
  const reply: FauxResponseFactory = (context) => {
    handle.appendResponses([reply]); // stay armed for the next request
    const messages = context.messages as { role: string; content?: unknown }[];
    const last = messages.at(-1);
    if (last?.role === "toolResult") return fauxAssistantMessage(fauxText(`Tool finished: ${textOf(last.content).slice(0, 200)}`));
    const prompt = textOf([...messages].reverse().find((m) => m.role === "user")?.content);
    if (prompt.startsWith("!"))
      return fauxAssistantMessage([fauxText("Running it."), fauxToolCall("bash", { command: prompt.slice(1).trim() })], { stopReason: "toolUse" });
    return fauxAssistantMessage(fauxText(`Echo: ${prompt}`));
  };
  handle.setResponses([reply]);
  models.setProvider(handle.provider);
}

// 读取仓库根目录的 .env(与 Python 版共用)。已存在的环境变量优先。
function loadDotEnv() {
  const file = fileURLToPath(new URL("../.env", import.meta.url));
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_]+)\s*=\s*(.*)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2];
  }
}
