// Shared setup. Every example calls openAgent() and gets a harness plus its root conversation.
import { mkdirSync, readFileSync, existsSync } from "node:fs";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall, type FauxResponseFactory } from "@earendil-works/pi-ai/providers/faux";
import { openrouterProvider } from "@earendil-works/pi-ai/providers/openrouter";
import { createRegistry, Harness } from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";

export const ctx = BACKGROUND_CONTEXT; // a Chord context that never cancels
export const DATA_DIR = new URL("../.data/", import.meta.url).pathname;
export const DB_FILE = `${DATA_DIR}agent.sqlite`;

loadDotEnv();
const PROVIDER = process.env.PROVIDER === "openrouter" ? "openrouter" : "faux";

/** The model choice stored in the conversation's pi.agent document. */
export const model =
  PROVIDER === "openrouter"
    ? { provider: "openrouter", modelId: process.env.MODEL ?? "anthropic/claude-sonnet-5.5" }
    : { provider: "faux", modelId: "echo" };

/** Open (or reopen) the one SQLite file. The same file means the same conversation. */
export async function openAgent() {
  mkdirSync(DATA_DIR, { recursive: true });
  const models = createModels();
  if (PROVIDER === "openrouter") models.setProvider(openrouterProvider());
  else installFaux(models);

  const registry = createRegistry();
  registry.install(CodingTools); // read, write, edit, bash

  const harness = await Harness.open(
    await openNodeSqliteStorage(DB_FILE),
    { models, registry, env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? DATA_DIR }) },
    ctx,
  );
  const root = await harness.root(ctx, { agent: { model, cwd: DATA_DIR } });
  return { harness, root };
}

/** Print every stored entry of a conversation: kind, id and a short text. */
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

// A free, deterministic model. "!<cmd>" makes one bash call. Anything else is echoed.
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

function loadDotEnv() {
  const file = new URL("../.env", import.meta.url).pathname;
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_]+)\s*=\s*(.*)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2];
  }
}
