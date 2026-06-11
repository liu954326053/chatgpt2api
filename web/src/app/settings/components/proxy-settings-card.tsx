"use client";

import { useState } from "react";
import { Link2, LoaderCircle, PlugZap, Save } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { testProxy, type ProxyTestResult } from "@/lib/api";

import { useSettingsStore } from "../store";

export function ProxySettingsCard() {
  const [isTesting, setIsTesting] = useState(false);
  const [testResult, setTestResult] = useState<ProxyTestResult | null>(null);
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setProxy = useSettingsStore((state) => state.setProxy);
  const saveConfig = useSettingsStore((state) => state.saveConfig);

  const proxy = config?.proxy ?? "";

  const handleTest = async () => {
    const candidate = proxy.trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTesting(true);
    setTestResult(null);
    try {
      const data = await testProxy(candidate);
      setTestResult(data.result);
      const total = Number(data.result.total || 1);
      const passed = Number(data.result.passed ?? (data.result.ok ? total : 0));
      const failed = Number(data.result.failed ?? (data.result.ok ? 0 : 1));
      if (data.result.ok) {
        toast.success(total > 1 ? `代理池可用：${passed}/${total} 通过` : `代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`);
      } else {
        toast.error(total > 1 ? `代理池部分不可用：${failed}/${total} 失败` : `代理不可用：${data.result.error ?? "未知错误"}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTesting(false);
    }
  };

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-6 p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
              <Link2 className="size-5 text-stone-600" />
            </div>
            <div>
              <h2 className="text-lg font-semibold tracking-tight">全局代理</h2>
              <p className="text-sm text-stone-500">为系统中的出站请求配置代理池，保存后会立即生效。</p>
            </div>
          </div>
          <Badge variant={proxy.trim() ? "success" : "secondary"} className="w-fit rounded-md px-2.5 py-1">
            {proxy.trim() ? `${proxy.split(/\r?\n/).filter((item) => item.trim()).length} 个代理` : "未配置"}
          </Badge>
        </div>

        {isLoadingConfig ? (
          <div className="flex items-center justify-center py-10">
            <LoaderCircle className="size-5 animate-spin text-stone-400" />
          </div>
        ) : (
          <>
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">代理池</label>
              <Textarea
                value={proxy}
                onChange={(event) => {
                  setProxy(event.target.value);
                  setTestResult(null);
                }}
                placeholder={`http://127.0.0.1:7890
user:pass@proxy.example.com:1463`}
                className="min-h-36 rounded-xl border-stone-200 bg-white font-mono text-xs"
              />
              <p className="text-sm text-stone-500">
                留空表示不使用代理。支持一行一个代理，未写协议时默认按 http:// 处理；出站请求会按代理池轮询使用。
              </p>
            </div>

            {testResult ? (
              <div
                className={`rounded-xl border px-4 py-3 text-sm leading-6 ${
                  testResult.ok
                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                    : "border-rose-200 bg-rose-50 text-rose-800"
                }`}
              >
                <div>
                  {Number(testResult.total || 1) > 1
                    ? `代理池测试：${testResult.passed ?? 0}/${testResult.total} 通过，${testResult.failed ?? 0} 失败`
                    : testResult.ok
                      ? `代理可用：HTTP ${testResult.status}，用时 ${testResult.latency_ms} ms`
                      : `代理不可用：${testResult.error ?? "未知错误"}（用时 ${testResult.latency_ms} ms）`}
                </div>
                {testResult.items?.length ? (
                  <div className="mt-2 max-h-32 overflow-auto border-t border-current/10 pt-2 text-xs">
                    {testResult.items.map((item, index) => (
                      <div key={`${item.url || index}-${index}`} className="truncate">
                        {index + 1}. {item.ok ? "✅" : "❌"} {item.url || "proxy"} {item.status ? `HTTP ${item.status}` : item.error || ""} · {item.latency_ms} ms
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                className="h-10 rounded-xl border-stone-200 bg-white px-5 text-stone-700"
                onClick={() => void handleTest()}
                disabled={isTesting || isLoadingConfig}
              >
                {isTesting ? <LoaderCircle className="size-4 animate-spin" /> : <PlugZap className="size-4" />}
                测试代理
              </Button>
              <Button
                className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
                onClick={() => void saveConfig()}
                disabled={isSavingConfig}
              >
                {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                保存配置
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
