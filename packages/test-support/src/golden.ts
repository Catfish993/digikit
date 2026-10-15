import fs from "node:fs";
import path from "node:path";

/** Compare VALUE with the JSON golden at FILE[name]. With env UPDATE_GOLDENS=1 the file is written instead
 * (a JSON object of name -> value, keys sorted). Throws on a mismatch or a missing golden. */
export function golden(file: string, name: string, value: unknown): void {
  let doc: Record<string, unknown> = {};
  try {
    doc = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
  }
  if (process.env.UPDATE_GOLDENS === "1") {
    doc[name] = value;
    const sorted = Object.fromEntries(Object.entries(doc).sort(([a], [b]) => (a < b ? -1 : 1)));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, `${JSON.stringify(sorted, null, 2)}\n`);
    return;
  }
  if (!(name in doc)) throw new Error(`no golden ${name} in ${file}; run with UPDATE_GOLDENS=1`);
  const want = JSON.stringify(doc[name]);
  const got = JSON.stringify(value);
  if (want !== got) throw new Error(`golden ${name} differs: want ${want}, got ${got}`);
}
