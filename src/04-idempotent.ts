// Lesson 4: the same requestId is admitted once. A retry (or a restart) finds the same submission.
import { ctx, openAgent } from "./setup.ts";

const { harness, root } = await openAgent();
const draft = { type: "input", content: "Say hi once.", requestId: "greeting-1" } as const;

const first = await root.submit(draft, ctx);
const second = await root.submit(draft, ctx);
console.log(`first.id=${first.id}  second.id=${second.id}  same=${first.id === second.id}`);

const settled = await first.wait(ctx);
console.log(`status: ${settled.status}`);
await harness.close(ctx);
