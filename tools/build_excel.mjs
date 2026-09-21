import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [csvPath, outputPath, month, year] = process.argv.slice(2);
if (!csvPath || !outputPath) throw new Error("Usage: build_excel.mjs input.csv output.xlsx month year");

const csvText = await fs.readFile(csvPath, "utf8");
const parsed = await Workbook.fromCSV(csvText, { sheetName: "YouTube plan" });
const workbook = parsed;
const sheet = workbook.worksheets.getItem("YouTube plan");
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(2);

const used = sheet.getUsedRange();
const rowCount = used.values.length;
const colCount = used.values[0].length;
sheet.getRangeByIndexes(0, 0, 1, colCount).format = {
  fill: "#12323E",
  font: { bold: true, color: "#FFFFFF", size: 11 },
  rowHeight: 34,
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "outside", style: "medium", color: "#0A2029" },
};
if (rowCount > 1) {
  sheet.getRangeByIndexes(1, 0, rowCount - 1, colCount).format = {
    font: { color: "#24343B", size: 10 },
    verticalAlignment: "top",
    wrapText: true,
    borders: { insideHorizontal: { style: "thin", color: "#E4E9EB" } },
  };
}

const widths = [10, 38, 29, 29, 24, 72, 24, 14, 34, 28];
for (let index = 0; index < Math.min(widths.length, colCount); index += 1) {
  sheet.getRangeByIndexes(0, index, rowCount, 1).format.columnWidth = widths[index];
}
if (rowCount > 1) sheet.getRangeByIndexes(1, 0, rowCount - 1, colCount).format.rowHeight = 58;

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const preview = await workbook.render({ sheetName: "YouTube plan", range: `A1:J${Math.min(rowCount, 20)}`, scale: 1.2, format: "png" });
await fs.writeFile(path.join(path.dirname(outputPath), `${path.parse(outputPath).name}_preview.png`), new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

const inspection = await workbook.inspect({ kind: "table", range: `YouTube plan!A1:J${Math.min(rowCount, 20)}`, include: "values,formulas", tableMaxRows: 20, tableMaxCols: 10 });
console.log(inspection.ndjson);
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 50 }, summary: "final formula error scan" });
console.log(errors.ndjson);
