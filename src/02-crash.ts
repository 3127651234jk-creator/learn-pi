// 第 2 课:发起一次要调用 4 秒 bash 工具的运行,然后在 1.5 秒后杀掉进程。
// 这次运行不会丢:它的状态都在 SQLite 里 —— 用户条目、工具调用、工具任务及其检查点。
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
  process.exit(1); // 不做任何清理,直接死
}, 1500);
