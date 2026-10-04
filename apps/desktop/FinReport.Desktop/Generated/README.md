# OpenAPI contracts

此目录只存放由运行中 API 的 `/openapi.json` 生成的 C# 合同。不要手写重复 DTO。

建议在具备 .NET SDK 的环境使用 NSwag 或 Kiota 生成，并在生成后由 `FinReportApiClient` 替换临时的最小解析代码。生成的合同必须随 API 契约一同审查和提交。
