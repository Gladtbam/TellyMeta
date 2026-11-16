/**
 * 将播放时长秒数格式化为可读中文时长字符串。
 * @param {number|null|undefined} seconds 播放时长秒数。
 * @returns {string} 格式化后的时长文本（如「1 小时 30 分钟」「45 秒」）。
 */
function formatWatchDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
        return minutes > 0 ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
    }
    if (minutes > 0) {
        return `${minutes} 分钟`;
    }
    return `${secs} 秒`;
}

/**
 * 格式化媒体账号的时长保活门槛进度文案（直接复用后端单源评估文本）。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {string} 保活进度、达标状态或未开放提示文本。
 */
function formatWatchTimeRenewalProgress(acc) {
    if (!acc || !acc.watch_time_renewal_open) {
        return acc && acc.watch_time_renewal_status_text
            ? acc.watch_time_renewal_status_text
            : '当前服务器未开放保活续期';
    }
    return acc.watch_time_renewal_status_text || formatWatchDuration(acc.cycle_watch_time_seconds);
}

/**
 * 格式化自动保活开关副标题说明，避免开放状态判断在多个页面重复实现。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @param {boolean} detailed 是否使用账号详情页的完整说明文案。
 * @returns {string} 自动保活提示文案。
 */
function formatAutoRenewHint(acc, detailed = false) {
    if (acc && acc.watch_time_renewal_open) {
        return detailed
            ? '到期前若本周期有效时长达标将自动免费续期'
            : '临近到期且达标时自动免费续期';
    }
    return detailed
        ? '当前服务器未开放保活续期（开启后暂不生效）'
        : '当前服务器未开放保活续期';
}

/**
 * 统一处理 Mini App 账号自动保活开关切换、错误回滚与结果提示。
 * @param {number|string} serverId 服务器 ID。
 * @param {object} tg Telegram WebApp 对象。
 * @param {HTMLInputElement|null} checkbox 触发切换的复选框元素。
 * @param {Function|null} onSuccess 可选成功回调，接收最终开关布尔状态。
 */
async function handleAutoRenewToggle(serverId, tg, checkbox, onSuccess = null) {
    const targetState = Boolean(checkbox && checkbox.checked);
    try {
        const res = await fetch(`/api/miniapp/accounts/${serverId}/toggle_auto_renew`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': tg.initData
            },
            body: JSON.stringify({ enabled: targetState })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            throw new Error(data.detail || data.message || '切换自动保活失败');
        }
        const finalState = typeof data.new_state === 'boolean' ? data.new_state : targetState;
        if (onSuccess) onSuccess(finalState, data);
        tg.showAlert(data.message || (finalState ? '已开启自动保活' : '已关闭自动保活'));
    } catch (e) {
        if (checkbox) checkbox.checked = !targetState;
        tg.showAlert(e.message || '切换自动保活失败');
    }
}

/**
 * 将字节数转换为 1024 进制可读流量字符串（B / KiB / MiB / GiB / TiB）。
 * @param {number|null|undefined} bytes 流量字节数。
 * @returns {string} 格式化后的流量文本。
 */
function formatTrafficBytes(bytes) {
    let size = Math.max(0, Number(bytes) || 0);
    if (size === 0) return '0.00 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
        size /= 1024;
        idx += 1;
    }
    return `${size.toFixed(2)} ${units[idx]}`;
}

/**
 * 格式化媒体账号的本周期基础流量进度与剩余配额说明。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {string} 流量消耗与配额汇总文案。
 */
function formatTrafficQuotaSummary(acc) {
    if (!acc) return '暂无数据';
    const usedText = formatTrafficBytes(acc.cycle_traffic_bytes);
    if (!acc.enable_traffic) {
        return acc.cycle_traffic_bytes > 0
            ? `已用 ${usedText}（当前未开启采集）`
            : '当前服务器未开启流量采集';
    }
    const quota = Number(acc.traffic_quota_bytes || 0);
    if (quota <= 0) {
        return `已用 ${usedText} / 不限`;
    }
    const remaining = acc.traffic_remaining_bytes !== null && acc.traffic_remaining_bytes !== undefined
        ? Number(acc.traffic_remaining_bytes)
        : Math.max(0, quota - Number(acc.cycle_traffic_bytes || 0));
    return `已用 ${usedText} / 配额 ${formatTrafficBytes(quota)}（剩余 ${formatTrafficBytes(remaining)}）`;
}

/**
 * 格式化下次锚定自然月重置日期为 YYYY-MM-DD。
 * @param {string|null|undefined} isoStr ISO 时间字符串。
 * @returns {string} 格式化后的日期字符串。
 */
function formatResetDate(isoStr) {
    if (!isoStr) return '未知';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return '未知';
    return `${d.getFullYear()}-${(d.getMonth() + 1).toString().padStart(2, '0')}-${d.getDate().toString().padStart(2, '0')}`;
}
