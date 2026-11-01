// 第 1 课:一问一答,并且已经落盘。
// 运行两次:第二次也会打印第一次的条目,因为 SQLite 文件就是会话本身。
import { ctx, DB_FILE, openAgent, printTranscript } from "./setup.ts";

const { harness, root } = await openAgent();

const submission = await root.submit({ type: "input", content: "What is a durable agent?" }, ctx);
const settled = await submission.wait(ctx);
console.log(`submission ${submission.id} is ${settled.status}`);

console.log(`\nEverything stored in ${DB_FILE}:`);
await printTranscript(root);
await harness.close(ctx);
