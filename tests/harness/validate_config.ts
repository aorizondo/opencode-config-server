// Validates configuration documents against the harness's own schema.
//
// Run by tests/test_harness_schema.py when an OpenCode v2 checkout is available.
// It must execute from inside <opencode>/packages/core so bun resolves `effect`
// and the workspace aliases. Every document is checked in one process: starting
// bun against that workspace is slow, so the test suite only does it once.
//
//   bun validate_config.ts <a.json> [<b.json> ...]   -> one JSON report per line
import { Schema, Option } from "effect"
import { Info } from "@opencode/schema/config"
import { ConfigNormalize } from "./src/config/normalize.js"

const decode = Schema.decodeUnknownOption(Info, { errors: "all", onExcessProperty: "ignore" })

for (const file of process.argv.slice(2)) {
  const report: Record<string, unknown> = { file }
  try {
    const result = ConfigNormalize.normalize(JSON.parse(await Bun.file(file).text()))
    report.normalize = result.type
    report.diagnostics = result.diagnostics.map((item) => ({
      kind: item.kind,
      path: `$.${item.path.join(".")}`,
      message: item.message,
    }))
    if (result.type !== "rejected") {
      const decoded = decode(result.encoded)
      report.decode = Option.isSome(decoded) ? "ok" : "failed"
      if (Option.isSome(decoded)) {
        const info = decoded.value as Record<string, any>
        report.keys = Object.keys(info)
        report.agents = Object.keys(info.agents ?? {})
        report.commands = Object.keys(info.commands ?? {})
        report.skills = info.skills ?? []
        report.permissions = (info.permissions ?? []).length
      }
    }
  } catch (error) {
    report.error = String(error)
  }
  console.log(JSON.stringify(report))
}
