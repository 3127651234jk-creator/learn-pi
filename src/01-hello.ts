// Lesson 1: one question, one answer, and it is on disk.
// Run it twice. The second run shows the first run's entries too, because the SQLite file is the conversation.
import { ctx, DB_FILE, openAgent, printTranscript } from "./setup.ts";

const { harness, root } = await openAgent();

const submission = await root.submit({ type: "input", content: "What is a durable agent?" }, ctx);
const settled = await submission.wait(ctx);
console.log(`submission ${submission.id} is ${settled.status}`);

console.log(`\nEverything stored in ${DB_FILE}:`);
await printTranscript(root);
await harness.close(ctx);
