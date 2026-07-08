import time
import json
import uuid
from datetime import datetime
from collections import deque
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Request Monitor"])

# Thread-safe in-memory cache for request logs. Max size = 200 to prevent memory leak.
MAX_LOGS = 200
request_store = deque(maxlen=MAX_LOGS)

@router.get("/api/system/request-logs")
async def get_logs(limit: int = 100):
    """Retrieve the captured request logs"""
    return list(request_store)[:limit]

@router.post("/api/system/request-logs/clear")
async def clear_logs():
    """Clear all request logs in the memory"""
    request_store.clear()
    return {"status": "success", "message": "All request logs have been cleared."}

@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve a high-fidelity interactive dashboard for monitoring HTTP requests in real-time"""
    html_content = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unified API Request Monitor Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['Outfit', 'sans-serif'],
                        mono: ['JetBrains Mono', 'monospace'],
                    },
                    colors: {
                        bgDark: '#0b0f19',
                        panelDark: '#121829',
                        primaryViolet: '#8b5cf6',
                        primarySky: '#06b6d4',
                    }
                }
            }
        }
    </script>
    <style>
        body {
            background-color: #0b0f19;
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.12) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(6, 182, 212, 0.12) 0%, transparent 40%);
            color: #e2e8f0;
            min-height: 100vh;
        }
        .glass-panel {
            background: rgba(18, 24, 41, 0.7);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
        }
        .neon-border-violet {
            box-shadow: 0 0 15px rgba(139, 92, 246, 0.25);
            border-color: rgba(139, 92, 246, 0.4);
        }
        .neon-border-sky {
            box-shadow: 0 0 15px rgba(6, 182, 212, 0.25);
            border-color: rgba(6, 182, 212, 0.4);
        }
        /* Custom Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.1);
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.2);
        }
        .log-row {
            transition: all 0.2s ease;
        }
        .log-row:hover {
            background: rgba(255, 255, 255, 0.04);
            transform: translateX(4px);
        }
        .active-log-row {
            background: rgba(139, 92, 246, 0.12) !important;
            border-left: 4px solid #8b5cf6 !important;
        }
        .drawer-slide {
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
    </style>
</head>
<body class="p-6 md:p-8 flex flex-col antialiased">

    <!-- Top Navigation Header -->
    <header class="flex justify-between items-center mb-8">
        <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-violet-600 to-cyan-500 flex items-center justify-center font-bold text-white shadow-lg shadow-violet-500/20">⚡</div>
            <div>
                <h1 class="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-violet-400 via-fuchsia-300 to-cyan-400">
                    Unified HTTP Request Monitor
                </h1>
                <p class="text-xs text-gray-400 mt-0.5">實時 API 效能與流量追蹤後台</p>
            </div>
        </div>

        <div class="flex items-center gap-3">
            <!-- Auto Refresh Control -->
            <div class="flex items-center gap-2 bg-slate-900/60 px-3 py-2 rounded-xl border border-white/5">
                <span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
                <span class="text-xs text-gray-300 font-medium">實時連線</span>
                <button id="toggle-refresh" onclick="toggleAutoRefresh()" class="ml-2 text-xs font-bold text-violet-400 hover:text-violet-300 transition">
                    暫停更新
                </button>
            </div>

            <!-- Clear logs button -->
            <button onclick="clearAllLogs()" class="bg-red-500/10 hover:bg-red-500/20 border border-red-500/25 px-4 py-2 rounded-xl font-bold text-red-400 text-sm shadow transition flex items-center gap-1.5 cursor-pointer">
                🗑️ 清空日誌
            </button>
        </div>
    </header>

    <!-- Metrics Cards Grid -->
    <section class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <!-- Card 1: Total Requests -->
        <div class="glass-panel rounded-2xl p-5 flex flex-col justify-between relative overflow-hidden">
            <div class="text-xs text-gray-400 uppercase tracking-wider font-semibold">總監控請求</div>
            <div class="flex items-baseline mt-4 gap-2">
                <span id="metric-total" class="text-4xl font-bold text-white">0</span>
                <span class="text-xs text-gray-500">reqs</span>
            </div>
            <div class="mt-2 text-xs text-violet-400 font-medium flex items-center gap-1">
                <span>🔄 自動即時捕捉已啟動</span>
            </div>
        </div>

        <!-- Card 2: Avg Latency -->
        <div class="glass-panel rounded-2xl p-5 flex flex-col justify-between relative overflow-hidden" id="card-latency">
            <div class="text-xs text-gray-400 uppercase tracking-wider font-semibold">平均響應時長</div>
            <div class="flex items-baseline mt-4 gap-2">
                <span id="metric-latency" class="text-4xl font-bold text-emerald-400">0.0</span>
                <span class="text-xs text-gray-500">ms</span>
            </div>
            <div class="mt-2 text-xs text-emerald-400/80 font-medium flex items-center gap-1">
                <span class="inline-block w-2 h-2 rounded-full bg-emerald-400" id="latency-dot"></span>
                <span id="latency-desc">連線速度極佳</span>
            </div>
        </div>

        <!-- Card 3: Success Rate -->
        <div class="glass-panel rounded-2xl p-5 flex flex-col justify-between relative overflow-hidden">
            <div class="text-xs text-gray-400 uppercase tracking-wider font-semibold">請求成功率</div>
            <div class="flex items-baseline mt-4 gap-2">
                <span id="metric-success-rate" class="text-4xl font-bold text-white">100</span>
                <span class="text-xs text-gray-500">%</span>
            </div>
            <div class="mt-2 text-xs text-cyan-400 font-medium flex items-center gap-1">
                <span id="metric-success-counts">0 成功 / 0 失敗</span>
            </div>
        </div>

        <!-- Card 4: Current Service State -->
        <div class="glass-panel rounded-2xl p-5 flex flex-col justify-between relative overflow-hidden">
            <div class="text-xs text-gray-400 uppercase tracking-wider font-semibold">主伺服器狀態</div>
            <div class="flex items-baseline mt-4 gap-2">
                <span class="text-2xl font-bold text-white uppercase flex items-center gap-2">
                    <span class="w-3.5 h-3.5 rounded-full bg-emerald-500 inline-block shadow shadow-emerald-500/50"></span>
                    ONLINE
                </span>
            </div>
            <div class="mt-2 text-xs text-gray-400 font-medium flex items-center gap-1 justify-between">
                <span>連接埠: 8000</span>
                <span>主機: 127.0.0.1</span>
            </div>
        </div>
    </section>

    <!-- Main Content Area: Logs & Controls -->
    <main class="flex-1 flex gap-6 overflow-hidden min-h-0">
        
        <!-- Left: Logs Section -->
        <div class="flex-1 glass-panel rounded-2xl flex flex-col overflow-hidden">
            
            <!-- Controls Bar -->
            <div class="p-4 border-b border-white/5 bg-slate-900/30 flex flex-wrap gap-4 items-center justify-between">
                
                <!-- Filter Group: Method -->
                <div class="flex gap-1.5 items-center">
                    <span class="text-xs text-gray-400 font-semibold mr-1">METHOD:</span>
                    <button onclick="setMethodFilter('ALL')" id="btn-method-ALL" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-violet-600 text-white shadow shadow-violet-600/20">ALL</button>
                    <button onclick="setMethodFilter('GET')" id="btn-method-GET" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-gray-300">GET</button>
                    <button onclick="setMethodFilter('POST')" id="btn-method-POST" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-gray-300">POST</button>
                </div>

                <!-- Filter Group: Status Code -->
                <div class="flex gap-1.5 items-center">
                    <span class="text-xs text-gray-400 font-semibold mr-1">STATUS:</span>
                    <button onclick="setStatusFilter('ALL')" id="btn-status-ALL" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-violet-600 text-white shadow shadow-violet-600/20">ALL</button>
                    <button onclick="setStatusFilter('2xx')" id="btn-status-2xx" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-emerald-400">2xx</button>
                    <button onclick="setStatusFilter('4xx')" id="btn-status-4xx" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-orange-400">4xx</button>
                    <button onclick="setStatusFilter('5xx')" id="btn-status-5xx" class="px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-red-400">5xx</button>
                </div>

                <!-- Live Search -->
                <div class="relative w-full md:w-64">
                    <span class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-gray-500 text-sm">🔍</span>
                    <input type="text" id="search-input" oninput="handleSearch()" placeholder="搜尋路徑或 IP..." class="w-full bg-slate-900/60 border border-white/10 rounded-xl pl-9 pr-4 py-2 text-sm focus:outline-none focus:border-violet-500 focus:ring-1 focus:ring-violet-500 text-white placeholder-gray-500 transition">
                </div>
            </div>

            <!-- Table Header -->
            <div class="grid grid-cols-12 px-6 py-3 bg-slate-950/40 text-xs font-semibold text-gray-400 border-b border-white/5 uppercase tracking-wider">
                <div class="col-span-2">時間</div>
                <div class="col-span-1">狀態碼</div>
                <div class="col-span-1">方法</div>
                <div class="col-span-4">請求路徑</div>
                <div class="col-span-2 text-right">回應耗時</div>
                <div class="col-span-2 text-right">來源 IP</div>
            </div>

            <!-- Log Rows Container -->
            <div id="logs-container" class="flex-1 overflow-y-auto divide-y divide-white/5">
                <!-- Loaded via JavaScript -->
                <div class="text-center py-20 text-gray-500 text-sm">⏳ 正在連線主系統並同步請求數據...</div>
            </div>
            
            <!-- Log Count Summary Bar -->
            <div class="p-3 bg-slate-950/20 border-t border-white/5 text-xs text-gray-500 flex justify-between">
                <div>快取池上限: 200 筆</div>
                <div id="metric-filtered-info">顯示 0 / 共 0 筆</div>
            </div>
        </div>

        <!-- Right: Inspector Sidebar Drawer (hidden by default) -->
        <div id="inspector-drawer" class="w-96 glass-panel rounded-2xl flex flex-col overflow-hidden hidden transform translate-x-4 opacity-0 transition-all duration-300">
            <!-- Drawer Header -->
            <div class="p-4 border-b border-white/5 bg-slate-900/30 flex justify-between items-center">
                <div class="flex items-center gap-2">
                    <span class="text-lg">🔍</span>
                    <h3 class="font-bold text-white text-base">請求詳情分析</h3>
                </div>
                <button onclick="closeInspector()" class="text-gray-400 hover:text-white transition p-1 bg-white/5 hover:bg-white/10 rounded-lg text-sm cursor-pointer">✕ 關閉</button>
            </div>

            <!-- Drawer Content (Scrollable) -->
            <div class="flex-1 p-5 overflow-y-auto space-y-5 text-sm">
                <!-- Summary Meta -->
                <div class="flex justify-between items-center p-3 rounded-xl bg-white/5 border border-white/5">
                    <div>
                        <div class="text-xs text-gray-400 uppercase font-semibold">Status</div>
                        <span id="inspect-status" class="inline-block mt-1 font-bold text-lg px-2.5 py-0.5 rounded-lg">200 OK</span>
                    </div>
                    <div class="text-right">
                        <div class="text-xs text-gray-400 uppercase font-semibold">Method</div>
                        <span id="inspect-method" class="inline-block mt-1 font-mono font-bold text-base px-2 py-0.5 rounded-lg bg-sky-500/10 text-sky-400">POST</span>
                    </div>
                </div>

                <!-- Details List -->
                <div class="space-y-3">
                    <div>
                        <div class="text-xs text-gray-400 font-semibold mb-1 uppercase tracking-wider">完整路徑</div>
                        <div id="inspect-path" class="font-mono text-xs bg-slate-950 p-2.5 rounded-lg break-all border border-white/5 text-gray-200">/api/chat</div>
                    </div>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <div class="text-xs text-gray-400 font-semibold mb-1 uppercase tracking-wider">回應耗時</div>
                            <div id="inspect-latency" class="font-mono text-xs font-bold text-emerald-400 bg-slate-950 p-2 rounded-lg border border-white/5">25.3 ms</div>
                        </div>
                        <div>
                            <div class="text-xs text-gray-400 font-semibold mb-1 uppercase tracking-wider">客戶端 IP</div>
                            <div id="inspect-ip" class="font-mono text-xs text-indigo-300 bg-slate-950 p-2 rounded-lg border border-white/5 text-center">127.0.0.1</div>
                        </div>
                    </div>
                    <div>
                        <div class="text-xs text-gray-400 font-semibold mb-1 uppercase tracking-wider">捕獲時間</div>
                        <div id="inspect-time" class="font-mono text-xs text-gray-300 bg-slate-950 p-2 rounded-lg border border-white/5">2026-05-30 14:40:00</div>
                    </div>
                </div>

                <!-- Query Parameters -->
                <div>
                    <div class="text-xs text-gray-400 font-semibold mb-1.5 uppercase tracking-wider">查詢參數 (Query Params)</div>
                    <pre id="inspect-query" class="font-mono text-xs text-sky-400 bg-slate-950/80 p-3 rounded-lg border border-white/5 max-h-40 overflow-y-auto whitespace-pre-wrap">{}</pre>
                </div>

                <!-- Request Headers -->
                <div>
                    <div class="text-xs text-gray-400 font-semibold mb-1.5 uppercase tracking-wider">Headers 快照</div>
                    <pre id="inspect-headers" class="font-mono text-xs text-indigo-400 bg-slate-950/80 p-3 rounded-lg border border-white/5 max-h-48 overflow-y-auto whitespace-pre-wrap">{}</pre>
                </div>

                <!-- Request Payload / Body -->
                <div>
                    <div class="text-xs text-gray-400 font-semibold mb-1.5 uppercase tracking-wider">請求內容 (Request Payload)</div>
                    <pre id="inspect-body" class="font-mono text-xs text-amber-400 bg-slate-950/80 p-3 rounded-lg border border-white/5 max-h-60 overflow-y-auto whitespace-pre-wrap">{}</pre>
                </div>
            </div>
        </div>

    </main>

    <!-- Script Block -->
    <script>
        let logsList = [];
        let filterMethod = 'ALL';
        let filterStatus = 'ALL';
        let filterSearch = '';
        let activeLogId = null;
        let autoRefreshInterval = null;
        let isRefreshing = true;

        // Fetch captured requests from API
        async function fetchLogs() {
            if (!isRefreshing) return;
            try {
                const res = await fetch('/api/system/request-logs');
                if (res.ok) {
                    logsList = await res.json();
                    renderDashboard();
                }
            } catch (e) {
                console.error("Dashboard error fetching request logs:", e);
            }
        }

        // Toggle Auto Refresh
        function toggleAutoRefresh() {
            isRefreshing = !isRefreshing;
            const btn = document.getElementById("toggle-refresh");
            const dot = document.querySelector(".bg-emerald-500");
            
            if (isRefreshing) {
                btn.innerText = "暫停更新";
                btn.classList.add("text-violet-400");
                btn.classList.remove("text-yellow-500");
                dot.classList.add("bg-emerald-500", "animate-pulse");
                dot.classList.remove("bg-yellow-500");
            } else {
                btn.innerText = "啟用更新";
                btn.classList.remove("text-violet-400");
                btn.classList.add("text-yellow-500");
                dot.classList.remove("bg-emerald-500", "animate-pulse");
                dot.classList.add("bg-yellow-500");
            }
        }

        // Clear All Logs
        async function clearAllLogs() {
            if (!confirm("確定要清空快取中的所有監控日誌嗎？")) return;
            try {
                const res = await fetch('/api/system/request-logs/clear', { method: 'POST' });
                if (res.ok) {
                    logsList = [];
                    activeLogId = null;
                    closeInspector();
                    renderDashboard();
                }
            } catch (e) { }
        }

        // Filters handlers
        function setMethodFilter(method) {
            filterMethod = method;
            document.querySelectorAll("[id^='btn-method-']").forEach(btn => {
                btn.className = "px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 text-gray-300";
            });
            const activeBtn = document.getElementById(`btn-method-${method}`);
            activeBtn.className = "px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-violet-600 text-white shadow shadow-violet-600/20";
            renderDashboard();
        }

        function setStatusFilter(status) {
            filterStatus = status;
            document.querySelectorAll("[id^='btn-status-']").forEach(btn => {
                let defaultClass = "px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-slate-800 hover:bg-slate-700 ";
                if (btn.id.endsWith("2xx")) btn.className = defaultClass + "text-emerald-400";
                else if (btn.id.endsWith("4xx")) btn.className = defaultClass + "text-orange-400";
                else if (btn.id.endsWith("5xx")) btn.className = defaultClass + "text-red-400";
                else btn.className = defaultClass + "text-gray-300";
            });
            const activeBtn = document.getElementById(`btn-status-${status}`);
            activeBtn.className = "px-3 py-1.5 rounded-lg text-xs font-bold transition cursor-pointer bg-violet-600 text-white shadow shadow-violet-600/20";
            renderDashboard();
        }

        function handleSearch() {
            filterSearch = document.getElementById("search-input").value.trim().toLowerCase();
            renderDashboard();
        }

        // Renders view
        function renderDashboard() {
            // Apply filtering
            const filteredLogs = logsList.filter(log => {
                // 1. Method Filter
                if (filterMethod !== 'ALL' && log.method !== filterMethod) return false;
                
                // 2. Status Filter
                if (filterStatus !== 'ALL') {
                    const code = log.status_code;
                    if (filterStatus === '2xx' && (code < 200 || code >= 300)) return false;
                    if (filterStatus === '4xx' && (code < 400 || code >= 500)) return false;
                    if (filterStatus === '5xx' && code < 500) return false;
                }

                // 3. Search Filter
                if (filterSearch) {
                    const matchPath = log.path.toLowerCase().includes(filterSearch);
                    const matchIp = log.client_ip.toLowerCase().includes(filterSearch);
                    if (!matchPath && !matchIp) return false;
                }
                return true;
            });

            // Update Metrics Dashboard
            const totalCount = logsList.length;
            document.getElementById("metric-total").innerText = totalCount;
            document.getElementById("metric-filtered-info").innerText = `顯示 ${filteredLogs.length} / 共 ${totalCount} 筆`;

            // Calculate Avg Latency & Success rate
            if (totalCount > 0) {
                const totalLatency = logsList.reduce((acc, curr) => acc + curr.latency_ms, 0);
                const avgLatency = (totalLatency / totalCount).toFixed(1);
                const avgEl = document.getElementById("metric-latency");
                avgEl.innerText = avgLatency;
                
                // Color average latency card dynamically
                const card = document.getElementById("card-latency");
                const dot = document.getElementById("latency-dot");
                const desc = document.getElementById("latency-desc");
                
                card.className = "glass-panel rounded-2xl p-5 flex flex-col justify-between relative overflow-hidden ";
                if (avgLatency < 100) {
                    avgEl.className = "text-4xl font-bold text-emerald-400";
                    dot.className = "inline-block w-2 h-2 rounded-full bg-emerald-400";
                    desc.className = "text-emerald-400/80 font-medium";
                    desc.innerText = "伺服器響應極速";
                } else if (avgLatency < 350) {
                    avgEl.className = "text-4xl font-bold text-amber-400";
                    dot.className = "inline-block w-2 h-2 rounded-full bg-amber-400";
                    desc.className = "text-amber-400/80 font-medium";
                    desc.innerText = "伺服器響應平穩";
                } else {
                    avgEl.className = "text-4xl font-bold text-rose-500";
                    dot.className = "inline-block w-2 h-2 rounded-full bg-rose-500";
                    desc.className = "text-rose-400/80 font-medium";
                    desc.innerText = "伺服器響應較慢";
                    card.classList.add("border-red-500/20", "bg-red-950/5");
                }

                // Success rate calculation
                const failureCount = logsList.filter(log => log.status_code >= 400).length;
                const successCount = totalCount - failureCount;
                const rate = ((successCount / totalCount) * 100).toFixed(0);
                document.getElementById("metric-success-rate").innerText = rate;
                document.getElementById("metric-success-counts").innerText = `${successCount} 成功 / ${failureCount} 異常`;
                const rateEl = document.getElementById("metric-success-rate");
                if (rate >= 95) rateEl.className = "text-4xl font-bold text-emerald-400";
                else if (rate >= 80) rateEl.className = "text-4xl font-bold text-amber-400";
                else rateEl.className = "text-4xl font-bold text-rose-500";
            } else {
                document.getElementById("metric-latency").innerText = "0.0";
                document.getElementById("metric-success-rate").innerText = "100";
                document.getElementById("metric-success-counts").innerText = "0 成功 / 0 失敗";
            }

            // Render Logs List
            const logsContainer = document.getElementById("logs-container");
            if (filteredLogs.length === 0) {
                logsContainer.innerHTML = `
                    <div class="text-center py-20 text-gray-500 text-sm">
                        📭 沒有符合過濾條件的監控請求日誌。
                    </div>
                `;
                return;
            }

            logsContainer.innerHTML = filteredLogs.map(log => {
                // Method styling
                let methodClass = "bg-slate-800 text-gray-400";
                if (log.method === 'GET') methodClass = "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
                else if (log.method === 'POST') methodClass = "bg-sky-500/10 text-sky-400 border border-sky-500/20";
                else if (log.method === 'DELETE') methodClass = "bg-red-500/10 text-red-400 border border-red-500/20";

                // Status styling
                let statusClass = "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
                if (log.status_code >= 500) statusClass = "bg-red-500/10 text-red-500 border border-red-500/30 font-bold";
                else if (log.status_code >= 400) statusClass = "bg-orange-500/10 text-orange-400 border border-orange-500/20";
                else if (log.status_code >= 300) statusClass = "bg-indigo-500/10 text-indigo-400 border border-indigo-500/20";

                // Latency coloring
                let latencyClass = "text-emerald-400";
                if (log.latency_ms > 300) latencyClass = "text-rose-400";
                else if (log.latency_ms > 100) latencyClass = "text-amber-400";

                const isSelected = log.id === activeLogId ? 'active-log-row border-l-4 border-violet-500' : '';

                return `
                    <div onclick="inspectLog('${log.id}')" class="grid grid-cols-12 px-6 py-3 text-sm log-row items-center cursor-pointer ${isSelected}">
                        <div class="col-span-2 text-gray-500 font-mono text-xs">${log.timestamp.split(' ')[1]}</div>
                        <div class="col-span-1">
                            <span class="px-2 py-0.5 rounded text-xs font-bold ${statusClass}">${log.status_code}</span>
                        </div>
                        <div class="col-span-1">
                            <span class="px-1.5 py-0.5 rounded font-mono text-xs font-bold ${methodClass}">${log.method}</span>
                        </div>
                        <div class="col-span-4 font-mono text-xs text-gray-200 truncate pr-4" title="${log.path}">
                            ${log.path}
                        </div>
                        <div class="col-span-2 text-right font-mono font-semibold ${latencyClass}">
                            ${log.latency_ms.toFixed(1)} <span class="text-[10px] text-gray-500">ms</span>
                        </div>
                        <div class="col-span-2 text-right font-mono text-xs text-indigo-300/80">
                            ${log.client_ip}
                        </div>
                    </div>
                `;
            }).join('');
        }

        // Details drawer inspection trigger
        function inspectLog(id) {
            activeLogId = id;
            const log = logsList.find(l => l.id === id);
            if (!log) return;

            // Highlight selected row locally
            renderDashboard();

            // Set content in inspect panel
            document.getElementById("inspect-path").innerText = log.path;
            document.getElementById("inspect-method").innerText = log.method;
            
            // Method styling in details
            const mEl = document.getElementById("inspect-method");
            mEl.className = "inline-block mt-1 font-mono font-bold text-sm px-2 py-0.5 rounded-lg ";
            if (log.method === 'GET') mEl.classList.add("bg-emerald-500/10", "text-emerald-400");
            else if (log.method === 'POST') mEl.classList.add("bg-sky-500/10", "text-sky-400");
            else mEl.classList.add("bg-slate-800", "text-gray-400");

            // Status code styling
            const sEl = document.getElementById("inspect-status");
            sEl.innerText = `${log.status_code}`;
            sEl.className = "inline-block mt-1 font-bold text-sm px-2.5 py-0.5 rounded-lg ";
            if (log.status_code >= 500) sEl.classList.add("bg-red-500/15", "text-red-500", "neon-border-red");
            else if (log.status_code >= 400) sEl.classList.add("bg-orange-500/10", "text-orange-400");
            else sEl.classList.add("bg-emerald-500/10", "text-emerald-400");

            document.getElementById("inspect-latency").innerText = `${log.latency_ms.toFixed(2)} ms`;
            document.getElementById("inspect-ip").innerText = log.client_ip;
            document.getElementById("inspect-time").innerText = log.timestamp;
            
            // Query params formatting
            try {
                document.getElementById("inspect-query").innerText = JSON.stringify(log.query_params, null, 2);
            } catch (e) {
                document.getElementById("inspect-query").innerText = "{}";
            }

            // Headers formatting
            try {
                document.getElementById("inspect-headers").innerText = JSON.stringify(log.headers, null, 2);
            } catch (e) {
                document.getElementById("inspect-headers").innerText = "{}";
            }

            // Request payload body formatting
            if (log.request_body) {
                try {
                    // Try parsing JSON if possible for nice printing
                    const parsed = JSON.parse(log.request_body);
                    document.getElementById("inspect-body").innerText = JSON.stringify(parsed, null, 2);
                } catch(e) {
                    document.getElementById("inspect-body").innerText = log.request_body;
                }
            } else {
                document.getElementById("inspect-body").innerText = "[Empty Payload / GET]";
            }

            // Open the drawer with smooth animation
            const drawer = document.getElementById("inspector-drawer");
            drawer.classList.remove("hidden");
            setTimeout(() => {
                drawer.classList.remove("translate-x-4", "opacity-0");
            }, 10);
        }

        // Close drawer
        function closeInspector() {
            const drawer = document.getElementById("inspector-drawer");
            drawer.classList.add("translate-x-4", "opacity-0");
            activeLogId = null;
            renderDashboard();
            setTimeout(() => {
                drawer.classList.add("hidden");
            }, 300);
        }

        // Initial loading loop
        fetchLogs();
        autoRefreshInterval = setInterval(fetchLogs, 1200);
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

class RequestLoggingMiddleware:
    """
    純 ASGI middleware（避免 Starlette BaseHTTPMiddleware 在 POST 時
    觸發 'Unexpected message received: http.request' 的已知 bug，
    該 bug 會導致 cloudflared 收到 unexpected EOF → 外網 520）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "")
        client_ip = scope.get("client", ["unknown"])[0] if scope.get("client") else "unknown"

        # Skip internal logger endpoints to prevent loops & pollution
        if path in ["/api/system/request-logs", "/api/system/request-logs/clear", "/dashboard", "/favicon.ico"] or path.startswith("/static"):
            return await self.app(scope, receive, send)

        # Snapshot headers & query params
        headers_snapshot = {}
        for k, v in scope.get("headers", []):
            try:
                key = k.decode("latin-1").lower()
                if key not in ["cookie", "authorization", "x-api-key"]:
                    headers_snapshot[key] = v.decode("latin-1", errors="ignore")
            except Exception:
                pass

        query_params = {}
        qs = scope.get("query_string", b"")
        if qs:
            try:
                from urllib.parse import parse_qs
                query_params = {k: v[0] if v else "" for k, v in parse_qs(qs.decode("latin-1")).items()}
            except Exception:
                pass

        start_time = time.perf_counter()

        # Buffer request body for POST/PUT/PATCH so downstream can still read it
        req_body_str = ""
        if method in ("POST", "PUT", "PATCH"):
            body_chunks = []
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.request":
                    chunk = message.get("body", b"")
                    if chunk:
                        body_chunks.append(chunk)
                    more_body = message.get("more_body", False)
                else:
                    # http.disconnect or unexpected — stop early
                    more_body = False

            body_bytes = b"".join(body_chunks)

            # Format body string safely (limit length to prevent memory bloom)
            if len(body_bytes) < 10000:
                req_body_str = body_bytes.decode("utf-8", errors="ignore")
            else:
                req_body_str = f"[Payload too large: {len(body_bytes)} bytes]"

            # Re-inject the buffered body downstream
            body_sent = False

            async def replay_receive():
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.disconnect"}
        else:
            replay_receive = receive

        # Capture status code by wrapping `send`
        status_holder = {"status": 200}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
            status_code = status_holder["status"]
        except Exception as e:
            latency = (time.perf_counter() - start_time) * 1000
            request_store.appendleft({
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "path": path,
                "query_params": query_params,
                "client_ip": client_ip,
                "headers": headers_snapshot,
                "request_body": req_body_str,
                "status_code": 500,
                "latency_ms": latency
            })
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000

        request_store.appendleft({
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": path,
            "query_params": query_params,
            "client_ip": client_ip,
            "headers": headers_snapshot,
            "request_body": req_body_str,
            "status_code": status_code,
            "latency_ms": latency_ms
        })


# Backwards-compatible alias: 可被舊程式碼 import，但建議改用 add_middleware(RequestLoggingMiddleware)
async def log_requests_middleware(request, call_next):
    """Deprecated: 改用 add_middleware(RequestLoggingMiddleware)（見 main.py）。
    保留給舊呼叫端，但不再使用 BaseHTTPMiddleware 路徑。"""
    return await call_next(request)
