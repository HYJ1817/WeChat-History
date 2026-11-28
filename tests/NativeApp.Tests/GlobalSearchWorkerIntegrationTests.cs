using System.Diagnostics;
using System.Text.Json;
using WechatMemory.NativeApp.Services;
using WechatMemory.NativeApp.Models;
using Xunit;

namespace NativeApp.Tests;

public sealed class GlobalSearchWorkerIntegrationTests
{
    [Theory]
    [InlineData("demo")]
    [InlineData("offline")]
    public async Task NativeWorkflowMatchesTheRealWorkerProtocolAndSourceLifecycle(string source)
    {
        string root = Path.Combine(Path.GetTempPath(), "wechat-native-real-search-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        string data = Path.Combine(root, "synthetic.json");
        File.WriteAllText(data, JsonSerializer.Serialize(new
        {
            version = 1,
            conversations = Enumerable.Range(0, 2).Select(conversation => new
            {
                id = "synthetic-" + conversation, name = "合成会话 " + conversation,
                kind = conversation == 0 ? "direct" : "group",
                messages = Enumerable.Range(conversation * 30, 30).Select(i => new
                {
                    id = "m-" + i, sender = "Straße 合成成员", type = "text", content = "needle 中文😀 " + i,
                    timestamp = $"2026-11-25T12:00:{i:D2}+08:00", is_outgoing = false,
                }),
            }),
        }));
        File.WriteAllText(Path.Combine(root, "invalid.json"), "{}");
        WorkerClient? worker = null;
        string? workerTemp = null;
        try
        {
            string? project = AppContext.BaseDirectory;
            while (project is not null && !File.Exists(Path.Combine(project, "global.json")))
                project = Directory.GetParent(project)?.FullName;
            Assert.NotNull(project);
            var info = new ProcessStartInfo(Path.Combine(project!, ".venv", "Scripts", "python.exe")) { WorkingDirectory = root };
            foreach (string arg in new[] { "-I", "-X", "utf8", "-u", Path.Combine(project!, "src", "PythonWorker", "worker.py"),
                "--demo", data, "--jobs-dir", Path.Combine(root, "jobs"), "--scenes-file", Path.Combine(root, "scenes.json"),
                "--model-root", Path.Combine(root, "models") }) info.ArgumentList.Add(arg);
            // Python's cleanup scans and optional state are isolated. The native
            // client's KnownFolder-created temp directory is separately GUID-owned;
            // we do not claim changing this environment redirects KnownFolder.
            info.Environment["LOCALAPPDATA"] = Path.Combine(root, "local");
            info.Environment["APPDATA"] = Path.Combine(root, "roaming");
            info.Environment["TEMP"] = info.Environment["TMP"] = root;
            worker = WorkerClient.Start(info);
            workerTemp = Assert.IsType<string>(worker.TempRootPath);
            Assert.StartsWith("worker-", Path.GetFileName(workerTemp));
            var hello = await worker.RequestAsync("hello", new { });
            var capabilities = hello.GetProperty("capabilities").EnumerateArray().Select(item => item.GetString()).ToArray();
            Assert.Contains("global_message_search", capabilities);
            Assert.Contains("message_context", capabilities);
            var list = await worker.RequestAsync("list_conversations", new { source = "demo" });
            string session = list.GetProperty("sourceSessionId").GetString()!;
            if (source == "offline")
            {
                var imported = await worker.RequestAsync("import_json", new { path = data });
                session = imported.GetProperty("sourceSessionId").GetString()!;
            }
            var workflow = new GlobalSearchWorkflow();
            var first = await workflow.SearchFirstPageAsync(worker, source, session,
                new GlobalSearchCriteria("needle", "2026-11-25", "2026-11-25", "desc"));
            Assert.Equal(60, first.Total);
            Assert.Equal(50, first.Items.Count);
            Assert.Equal(50, first.NextOffset);
            Assert.Equal("m-59", first.Items[0].MessageId); // Preserve worker precise-v1 order.
            Assert.Equal("", first.CoverageNotice);

            await Assert.ThrowsAsync<WorkerException>(() => worker.RequestAsync("import_json", new { path = Path.Combine(root, "invalid.json") }));
            var all = await workflow.SearchNextPageAsync(worker, source, first);
            Assert.Equal(60, all.Items.Count);
            Assert.Null(all.NextOffset);
            Assert.Equal(60, all.Items.Select(item => item.HitId).Distinct().Count());
            var context = await workflow.GetContextAsync(worker, source, all, first.Items[0]);
            Assert.Equal("m-59", context.Items[context.FocusIndex].Id);
            Assert.True(context.Items[context.FocusIndex].IsContextFocus);
            Assert.All(context.Items, item => Assert.Null(item.AttachmentPath));
            Assert.InRange(context.Items.Count, 1, 41);

            var replacement = await worker.RequestAsync("import_json", new { path = data });
            Assert.NotEqual(session, replacement.GetProperty("sourceSessionId").GetString());
            var expired = await Assert.ThrowsAsync<WorkerException>(() => workflow.SearchNextPageAsync(worker, source, first));
            Assert.Equal("SOURCE_SESSION_CHANGED", expired.Code);
            Assert.True(worker.IsAvailable);
        }
        finally
        {
            if (worker is not null) await worker.DisposeAsync();
            if (workerTemp is not null) Assert.False(Directory.Exists(workerTemp));
            Directory.Delete(root, recursive: true);
        }
    }
}
