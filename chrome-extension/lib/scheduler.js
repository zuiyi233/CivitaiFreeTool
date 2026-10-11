// CFT.scheduler —— 按内容类型分池的下载调度器（搬运自油猴脚本 v1.7.8，逻辑原样保留）
// 搬运：_sleep(653) → sleep；_runPool(654-670) → runPool；createDownloadScheduler(1251-1353) 原样。
// 约束：纯逻辑，顶层不触碰 document/window/chrome。
globalThis.CFT = globalThis.CFT || {};

const _sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function _runPool(items, worker, maxConcurrent) {
    const results = new Array(items.length);
    let cursor = 0;
    async function drain() {
        while (true) {
            const i = cursor++;
            if (i >= items.length) return;
            results[i] = await worker(items[i], i);
        }
    }
    const n = Math.max(1, Math.min(maxConcurrent || 1, items.length));
    const runners = [];
    for (let k = 0; k < n; k++) runners.push(drain());
    await Promise.all(runners);
    return results;
}

const DL_TYPES = ['model', 'video', 'image', 'other'];
const DL_TYPE_LABEL = { model: '模型文件', video: '视频', image: '图片', other: '其他(文本/信息)' };

function createDownloadScheduler(opts) {
    opts = opts || {};
    const order = (Array.isArray(opts.order) && opts.order.length)
        ? opts.order.filter(t => DL_TYPES.indexOf(t) >= 0)
        : ['model', 'video', 'image', 'other'];
    const conc = Object.assign({ model: 1, video: 3, image: 6, other: 2 }, opts.concurrencyByType || {});
    const globalMax = Math.max(1, Number(opts.globalMax) || 8);
    const onTaskDone = (typeof opts.onTaskDone === 'function') ? opts.onTaskDone : null;
    const onError = (typeof opts.onError === 'function') ? opts.onError : null;

    const queues = {};   // type -> [task]
    const running = {};  // type -> 当前并发数
    const counters = {}; // type -> { done, total, fail }
    DL_TYPES.forEach(t => { queues[t] = []; running[t] = 0; counters[t] = { done: 0, total: 0, fail: 0 }; });
    const tasks = [];
    let drainedAll = false;
    const waiters = [];

    function priorityOf(type) {
        const i = order.indexOf(type);
        return i < 0 ? order.length : i; // 未列出者排最后
    }

    // 选择下一个可派发的类型：优先最高优先级，且未达该类型并发上限、全局未饱和
    function pickNextType() {
        let globalRunning = 0;
        DL_TYPES.forEach(t => { globalRunning += running[t]; });
        if (globalRunning >= globalMax) return null;
        let best = null, bestPri = Infinity;
        DL_TYPES.forEach(t => {
            if (!queues[t].length) return;
            if (running[t] >= (conc[t] || 1)) return;
            const pri = priorityOf(t);
            if (pri < bestPri) { bestPri = pri; best = t; }
        });
        return best;
    }

    async function exec(task) {
        running[task.type] += 1;
        task.status = 'running';
        try {
            await task.run(task);
            task.status = 'done';
            counters[task.type].done += 1;
        } catch (e) {
            task.status = 'error';
            task.error = e;
            counters[task.type].fail += 1;
            if (onError) { try { onError(task.type, e); } catch (_) { /* 忽略回调异常 */ } }
            console.error('[scheduler] 任务失败 type=' + task.type, e);
        } finally {
            running[task.type] -= 1;
            if (onTaskDone) { try { onTaskDone(task); } catch (_) { /* 忽略回调异常 */ } }
            pump();
        }
    }

    // 持续派发，直到没有可派发的类型
    function pump() {
        let t;
        while ((t = pickNextType())) {
            const task = queues[t].shift();
            exec(task); // 异步启动，不阻塞当前 pump
        }
        maybeResolve();
    }

    function maybeResolve() {
        if (!drainedAll) return;
        const allSettled = tasks.length > 0 && tasks.every(t => t.status === 'done' || t.status === 'error');
        if (allSettled) { while (waiters.length) waiters.shift()(); }
    }

    function enqueue(task) {
        const type = DL_TYPES.indexOf(task.type) >= 0 ? task.type : 'other';
        task.type = type;
        task.status = 'queued';
        counters[type].total += 1;
        tasks.push(task);
        queues[type].push(task);
        pump();
    }

    function waitAll() {
        return new Promise(resolve => {
            drainedAll = true;
            // 没有任何下载任务时（例如未开模型文件、无封面无配图）立即返回，避免永久挂起
            if (tasks.length === 0 || tasks.every(t => t.status === 'done' || t.status === 'error')) resolve();
            else waiters.push(resolve);
        });
    }

    function stats() {
        const s = { globalRunning: 0, byType: {} };
        DL_TYPES.forEach(t => { s.globalRunning += running[t]; s.byType[t] = Object.assign({}, counters[t], { running: running[t] }); });
        return s;
    }

    return { enqueue, waitAll, stats, DL_TYPES };
}

globalThis.CFT.scheduler = {
    createDownloadScheduler,
    runPool: _runPool,
    sleep: _sleep,
    DL_TYPES,
    DL_TYPE_LABEL,
};
