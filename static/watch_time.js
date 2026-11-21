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
 * 计算媒体账号当前周期观看度百分比数值（0~100，未达标时封顶 99.9）。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {number} 观看度百分比数值。
 */
function resolveWatchProgressPercent(acc) {
    if (!acc || !acc.watch_time_renewal_open) return 0;
    if (acc.watch_time_progress_percent !== null && acc.watch_time_progress_percent !== undefined) {
        const raw = Number(acc.watch_time_progress_percent);
        if (Number.isFinite(raw)) {
            return Math.max(0, Math.min(100, raw));
        }
    }
    if (acc.watch_time_renewal_eligible) return 100;
    const threshold = Math.max(0, Number(acc.watch_time_renewal_threshold_seconds || 0));
    const current = Math.max(0, Number(acc.cycle_watch_time_seconds || 0));
    if (threshold <= 0 || current <= 0) return 0;
    if (current >= threshold) return 100;
    return Math.min(99.9, Math.floor((current * 1000) / threshold) / 10);
}

/**
 * 格式化观看度百分比文本（整数不带小数，小数保留 1 位）。
 * @param {number} percent 观看度百分比数值。
 * @returns {string} 形如「50%」或「99.9%」的百分比字符串。
 */
function formatWatchProgressPercent(percent) {
    const val = Math.max(0, Math.min(100, Number(percent) || 0));
    return Number.isInteger(val) ? `${val}%` : `${val.toFixed(1)}%`;
}

/**
 * 格式化媒体账号的时长保活门槛进度文案（直接复用后端单源百分比评估文本，不暴露具体时长）。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {string} 观看度达标状态或未开放提示文本。
 */
function formatWatchTimeRenewalProgress(acc) {
    if (!acc || !acc.watch_time_renewal_open) {
        return acc && acc.watch_time_renewal_status_text
            ? acc.watch_time_renewal_status_text
            : '当前服务器未开放保活续期';
    }
    return acc.watch_time_renewal_status_text || `观看度 ${formatWatchProgressPercent(resolveWatchProgressPercent(acc))}`;
}

/**
 * 渲染用户侧媒体账号“观看度”百分比进度条 HTML；未开放保活或门槛为 0 时返回空字符串。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @param {boolean} showStatusNote 是否附带后端状态说明副标题。
 * @returns {string} 进度条行 HTML 字符串。
 */
function renderWatchProgressRow(acc, showStatusNote = true) {
    if (!acc || !acc.watch_time_renewal_open) return '';
    const pct = resolveWatchProgressPercent(acc);
    const pctLabel = formatWatchProgressPercent(pct);
    const eligible = Boolean(acc.watch_time_renewal_eligible);
    const cls = eligible ? ' is-eligible' : '';
    const statusLine = showStatusNote
        ? `<span class="item-subtitle" style="font-size:12px; margin-top:6px;">${formatWatchTimeRenewalProgress(acc)}</span>`
        : '';
    return `
                        <div class="item">
                            <div class="item-content">
                                <div class="watch-progress-header">
                                    <span class="item-subtitle" style="margin-top:0;">观看度</span>
                                    <span class="watch-progress-pct${cls}">${pctLabel}${eligible ? ' · 已达标' : ''}</span>
                                </div>
                                <div class="watch-progress-track">
                                    <div class="watch-progress-fill${cls}" style="width:${pct}%"></div>
                                </div>
                                ${statusLine}
                            </div>
                        </div>`;
}

/**
 * 判断媒体账号所在服务器是否至少开放一种自动续期条件（时长保活或积分续期）。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {boolean} 可开启自动续期返回 true，否则返回 false。
 */
function isAutoRenewOpen(acc) {
    if (!acc) return false;
    if (acc.auto_renew_open !== undefined && acc.auto_renew_open !== null) {
        return Boolean(acc.auto_renew_open);
    }
    return Boolean(acc.watch_time_renewal_open || acc.can_points_renew);
}

/**
 * 格式化自动续期开关副标题说明，避免开放状态判断在多个页面重复实现。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @param {boolean} detailed 是否使用账号详情页的完整说明文案。
 * @returns {string} 自动续期提示文案。
 */
function formatAutoRenewHint(acc, detailed = false) {
    const watchOpen = Boolean(acc && acc.watch_time_renewal_open);
    const pointsOpen = Boolean(acc && acc.can_points_renew);
    if (watchOpen && pointsOpen) {
        return detailed
            ? '到达有效期时优先按观看度 100% 免费续期，未达标时自动消耗积分续期'
            : '到期时优先时长免分续期，未达标则自动扣分续期';
    }
    if (watchOpen) {
        return detailed
            ? '到达有效期时若本周期观看度达 100% 将自动免费续期'
            : '到期时观看度达 100% 自动免费续期';
    }
    if (pointsOpen) {
        return detailed
            ? '到达有效期时将自动消耗积分续期（积分不足则过期封禁）'
            : '到期时自动消耗积分续期';
    }
    return detailed
        ? '当前服务器未开放可自动触发的续期条件（开启后暂不生效）'
        : '当前服务器未开放可自动触发的续期条件';
}

/**
 * 统一处理 Mini App 账号自动续期开关切换、错误回滚与结果提示。
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
            throw new Error(data.detail || data.message || '切换自动续期失败');
        }
        const finalState = typeof data.new_state === 'boolean' ? data.new_state : targetState;
        if (onSuccess) onSuccess(finalState, data);
        tg.showAlert(data.message || (finalState ? '已开启自动续期' : '已关闭自动续期'));
    } catch (e) {
        if (checkbox) checkbox.checked = !targetState;
        tg.showAlert(e.message || '切换自动续期失败');
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
    const packBytes = Math.max(0, Number(acc.traffic_pack_bytes || 0));
    const packSuffix = packBytes > 0 ? ` · 流量包 ${formatTrafficBytes(packBytes)}` : '';
    const adjBytes = Number(acc.cycle_traffic_adjustment_bytes || 0);
    const adjSign = adjBytes > 0 ? '+' : '-';
    const adjSuffix = adjBytes !== 0 ? `（含当期临时调额 ${adjSign}${formatTrafficBytes(Math.abs(adjBytes))}）` : '';
    const suspendSuffix = acc.is_traffic_suspended ? '（已超流停用）' : '';
    if (!acc.enable_traffic) {
        return acc.cycle_traffic_bytes > 0 || packBytes > 0
            ? `已用 ${usedText}${packSuffix}（当前未开启采集）${suspendSuffix}`
            : '当前服务器未开启流量采集';
    }
    const quota = Number(acc.traffic_quota_bytes || 0);
    if (quota <= 0) {
        return `已用 ${usedText} / 不限${packSuffix}`;
    }
    const remaining = acc.traffic_remaining_bytes !== null && acc.traffic_remaining_bytes !== undefined
        ? Number(acc.traffic_remaining_bytes)
        : Math.max(0, quota - Number(acc.cycle_traffic_bytes || 0));
    return `已用 ${usedText} / 配额 ${formatTrafficBytes(quota)}（基础剩余 ${formatTrafficBytes(remaining)}）${adjSuffix}${packSuffix}${suspendSuffix}`;
}

/**
 * 根据媒体账号组合禁用状态（过期封禁 OR 超流停用）解析状态标签颜色。
 * @param {object|null|undefined} acc 媒体账号 DTO 对象。
 * @returns {string} CSS 状态颜色值。
 */
function resolveAccountStatusColor(acc) {
    return acc && (acc.is_banned || acc.is_traffic_suspended)
        ? 'var(--destructive-text-color)'
        : '#34c759';
}

/**
 * 统一处理 Mini App 积分兑换流量包确认、幂等键投递与兑换后刷新。
 * @param {number|string} serverId 服务器 ID。
 * @param {object} tg Telegram WebApp 对象。
 * @param {object} acc 当前媒体账号 DTO 对象。
 * @param {object} packConfig 当前服流量包配置（enabled、costScore、sizeGib）。
 * @param {Function|null} onSuccess 可选成功回调。
 */
async function handleRedeemTrafficPack(serverId, tg, acc, packConfig, onSuccess = null) {
    if (packConfig && packConfig.enabled === false) {
        tg.showAlert('当前未开放积分兑换流量包功能。');
        return;
    }
    const costScore = Number(packConfig && packConfig.costScore) > 0 ? Number(packConfig.costScore) : 50;
    const sizeGib = Number(packConfig && packConfig.sizeGib) > 0 ? Number(packConfig.sizeGib) : 10;
    const resetHint = acc && acc.next_reset_at ? formatResetDate(acc.next_reset_at) : '下次重置日';
    let unsuspendHint = '';
    if (acc && acc.is_traffic_suspended) {
        unsuspendHint = acc.is_banned
            ? '\n兑换成功后将解除超流停用；由于账号同时处于过期封禁，需续期后才能恢复播放权限。'
            : '\n兑换成功后将立即为您解除超流停用并恢复播放权限。';
    }
    if (!confirm(`确认消耗 ${costScore} 积分兑换 ${sizeGib} GiB 当期流量包吗？\n流量包仅当期有效，将在 ${resetHint} 本周期重置清零。${unsuspendHint}`)) {
        return;
    }

    try {
        tg.MainButton.text = '正在兑换流量包...';
        tg.MainButton.show();
        tg.MainButton.showProgress();

        const idempotencyKey = (window.crypto && crypto.randomUUID)
            ? crypto.randomUUID()
            : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const res = await fetch(`/api/miniapp/accounts/${serverId}/redeem_traffic_pack`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': tg.initData,
                'X-Idempotency-Key': idempotencyKey
            },
            body: JSON.stringify({ quantity: 1 })
        });
        const data = await res.json();
        tg.MainButton.hideProgress();
        tg.MainButton.hide();

        if (!res.ok || !data.success) {
            throw new Error(data.detail || data.message || '兑换流量包失败');
        }
        tg.showAlert(data.message || '流量包兑换成功');
        if (onSuccess) onSuccess(data);
    } catch (e) {
        tg.MainButton.hideProgress();
        tg.MainButton.hide();
        tg.showAlert(e.message || '兑换流量包失败');
    }
}

/**
 * 从用户概览 DTO 中统一计算个人总览区各经济指标行的可见性布尔标志。
 * @param {object|null|undefined} data 用户概览 DTO 对象。
 * @returns {{pointsOn: boolean, checkinOn: boolean, renewCostOn: boolean, watchTimeOn: boolean}} 各维度显隐标志。
 */
function resolveVisibleEconomyFlags(data) {
    const pointsOn = Boolean(data && data.enable_points !== false);
    const checkinOn = Boolean(pointsOn && data && data.enable_points_checkin !== false);
    const renewCostOn = Boolean(
        pointsOn && data && (data.enable_points_renew !== false || data.enable_points_register !== false)
    );
    const watchTimeOn = Boolean(data && data.enable_watch_time);
    return { pointsOn, checkinOn, renewCostOn, watchTimeOn };
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
