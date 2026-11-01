// 第 4 课:同一个 requestId 只受理一次。重试(或重启)找到的是同一个 submission。
import { ctx, openAgent } from "./setup.ts";

const { harness, root } = await openAgent();
const draft = { type: "input", content: "Say hi once.", requestId: "greeting-1" } as const;

const first = await root.submit(draft, ctx);
const second = await root.submit(draft, ctx);
console.log(`first.id=${first.id}  second.id=${second.id}  same=${first.id === second.id}`);

const settled = await first.wait(ctx);
console.log(`status: ${settled.status}`);
await harness.close(ctx);
