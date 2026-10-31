// Lesson 5: a UI is just a watcher of committed state. Here we stream agent events while a steer joins the run.
import { watchEvents } from "@earendil-works/pi-durable";
import { ctx, openAgent } from "./setup.ts";

const { harness, root } = await openAgent();

const stream = await watchEvents(harness, root.id, ctx);
console.log(`snapshot: ${stream.snapshot.entries.length} entries already stored`);
stream.start(async (events) => {
  for (const e of events) {
    if (e.type === "message_update" || e.type === "tool_execution_update") continue; // too chatty
    console.log(`  event ${e.type}`);
  }
});

const run = await root.submit({ type: "input", content: "!sleep 2 && echo first" }, ctx);
await root.submit({ type: "input", content: "Also say thanks.", whenBusy: "steer" }, ctx); // joins after the tool round
await run.wait(ctx);
await root.waitForIdle(ctx);

await stream.stop();
await harness.close(ctx);
