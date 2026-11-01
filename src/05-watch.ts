// 第 5 课:UI 只是已提交状态的观察者。这里在 steer 加入运行时流式观察事件。
import { watchEvents } from "@earendil-works/pi-durable";
import { ctx, openAgent } from "./setup.ts";

const { harness, root } = await openAgent();

const stream = await watchEvents(harness, root.id, ctx);
console.log(`snapshot: ${stream.snapshot.entries.length} entries already stored`);
stream.start(async (events) => {
  for (const e of events) {
    if (e.type === "message_update" || e.type === "tool_execution_update") continue; // 太啰嗦
    console.log(`  event ${e.type}`);
  }
});

const run = await root.submit({ type: "input", content: "!sleep 2 && echo first" }, ctx);
await new Promise((r) => setTimeout(r, 500)); // 让运行先明显忙起来
await root.submit({ type: "input", content: "Also say thanks.", whenBusy: "steer" }, ctx); // 在当前工具轮次之后加入
await run.wait(ctx);
await root.waitForIdle(ctx);

await stream.stop();
await harness.close(ctx);
