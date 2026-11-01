// 第 3 课:重新打开同一个 SQLite 文件,让 harness 继续死掉的进程留下的运行。
// bash 不是重放安全的,所以模型收到一条 interrupted 工具结果,由它决定下一步。
import { readFileSync } from "node:fs";
import { ctx, DATA_DIR, openAgent, printTranscript } from "./setup.ts";

const { harness, root } = await openAgent();
const { id } = JSON.parse(readFileSync(`${DATA_DIR}last-submission.json`, "utf8"));

console.log(`reopened ${DATA_DIR}agent.sqlite; resuming unfinished work…`);
harness.resume(); // 启动任务调度:待处理的任务从各自检查点继续

const submission = await harness.submission(id, ctx); // 按 id 重新取得那个 submission
const settled = await submission!.wait(ctx);
console.log(`submission ${id} is now ${settled.status}\n`);

await printTranscript(root);
await harness.close(ctx);
