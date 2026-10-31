// Lesson 2: start a run that calls a 4 second bash tool, then kill the process after 1.5 seconds.
// The run is NOT lost. Its state is in SQLite: the user entry, the tool call, the tool task and its checkpoint.
import { writeFileSync } from "node:fs";
import { ctx, DATA_DIR, openAgent } from "./setup.ts";

const { root } = await openAgent();

const submission = await root.submit(
  { type: "input", content: "!sleep 4 && echo FINISHED", requestId: "crash-demo-1" },
  ctx,
);
writeFileSync(`${DATA_DIR}last-submission.json`, JSON.stringify({ id: submission.id }));
console.log(`submitted ${submission.id}; the bash tool is now running for 4 s`);

setTimeout(() => {
  console.log("1.5 s passed. Killing the process mid-tool. No close(), no cleanup.");
  process.exit(1);
}, 1500);
