// Lesson 3: reopen the same SQLite file and let the harness continue the run the dead process left behind.
// bash is not replay-safe, so the model gets an "interrupted" tool result and decides what to do next.
import { readFileSync } from "node:fs";
import { ctx, DATA_DIR, openAgent, printTranscript } from "./setup.ts";

const { harness, root } = await openAgent();
const { id } = JSON.parse(readFileSync(`${DATA_DIR}last-submission.json`, "utf8"));

console.log(`reopened ${DATA_DIR}agent.sqlite; resuming unfinished work…`);
harness.resume(); // starts the task scheduler: pending tasks pick up from their last checkpoint

const submission = await harness.submission(id, ctx); // reacquire the submission by id
const settled = await submission!.wait(ctx);
console.log(`submission ${id} is now ${settled.status}\n`);

await printTranscript(root);
await harness.close(ctx);
