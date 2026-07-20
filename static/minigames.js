// Minigames JavaScript

let userWallet = null;

document.addEventListener('DOMContentLoaded', async () => {
    console.log('🎮 Minigames page loaded');
    await loadUserStats();
});

async function loadUserStats() {
    try {
        const response = await fetch('/minigames/api/user-stats');
        const data = await response.json();

        if (data.success) {
            userWallet = data.user_wallet;
            const walletBalanceEl = document.getElementById('wallet-balance');
            if (walletBalanceEl) {
                walletBalanceEl.textContent = `${userWallet.toFixed(2)} G$`;
            }

            await updateTotalEarned();
        } else {
            console.error('❌ Failed to load user stats:', data.error);
        }
    } catch (error) {
        console.error('❌ Error loading user stats:', error);
    }
}

async function updateTotalEarned() {
    console.log('📊 Fetching total earned from all sources...');

    const learnEarnResponse = await fetch('/learn-earn/quiz-history?limit=1000');
    const learnEarnData = await learnEarnResponse.json();

    const telegramResponse = await fetch('/api/daily-task/history?limit=1000');
    const telegramData = await telegramResponse.json();

    const twitterResponse = await fetch('/api/twitter-task/transaction-history?limit=1000');
    const twitterData = await twitterResponse.json();

    let learnEarnTotal = 0;
    if (learnEarnData.quiz_history && Array.isArray(learnEarnData.quiz_history)) {
        learnEarnTotal = learnEarnData.quiz_history.reduce((sum, quiz) => {
            return sum + (parseFloat(quiz.amount_g$) || 0);
        }, 0);
    }

    let telegramTotal = 0;
    if (telegramData.success && telegramData.transactions) {
        telegramTotal = telegramData.transactions
            .filter(tx => tx.status === 'completed')
            .reduce((sum, tx) => sum + (parseFloat(tx.reward_amount) || 0), 0);
    }

    let twitterTotal = 0;
    if (twitterData.success && twitterData.transactions) {
        twitterTotal = twitterData.transactions
            .filter(tx => tx.status === 'completed')
            .reduce((sum, tx) => sum + (parseFloat(tx.reward_amount) || 0), 0);
    }

    const totalEarned = learnEarnTotal + telegramTotal + twitterTotal;
    console.log('✅ Total Earned Calculated:', totalEarned, 'G$');

    const totalEarnedEl = document.getElementById('total-earned');
    if (totalEarnedEl) {
        totalEarnedEl.textContent = totalEarned.toFixed(2) + ' G$';
    }
}

window.closeGameModal = function() {
    const modal = document.getElementById('gameModal');
    if (modal) modal.style.display = 'none';
    const content = document.getElementById('gameContent');
    if (content) content.innerHTML = '';
};

function showNotification(message, type = 'info') {
    const notification = document.getElementById('notification');
    if (!notification) {
        console.error('Notification element not found!');
        return;
    }
    notification.textContent = message;
    notification.style.display = 'block';
    notification.style.background = type === 'success' ? 'rgba(16, 185, 129, 0.95)' :
                                   type === 'error' ? 'rgba(239, 68, 68, 0.95)' :
                                   'rgba(99, 102, 241, 0.95)';

    setTimeout(() => {
        notification.style.display = 'none';
    }, 3000);
}

let coinClickState = null;

window.startCoinClick = async function() {
    try {
        const limitRes = await fetch('/minigames/api/check-limit/coin_click');
        const limitData = await limitRes.json();
        if (!limitData.success || !limitData.limit_check?.can_play) {
            showNotification(limitData.error || 'Daily CoinClick play limit reached. Come back tomorrow!', 'error');
            return;
        }

        const res = await fetch('/minigames/api/start-game', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ game_type: 'coin_click' })
        });
        const data = await res.json();
        if (!data.success) {
            showNotification(data.error || 'Unable to start CoinClick.', 'error');
            return;
        }

        openCoinClickModal(data.session_id, data.config, limitData.limit_check);
    } catch (error) {
        console.error('CoinClick start error:', error);
        showNotification('Network error while starting CoinClick.', 'error');
    }
};

function openCoinClickModal(sessionId, config, limitCheck) {
    const modal = document.getElementById('gameModal');
    const content = document.getElementById('gameContent');
    if (!modal || !content) return;

    const duration = config.duration_seconds || 37;
    content.innerHTML = `
        <div style="text-align:center; clear: both;">
            <h2 style="color:#fbbf24; margin-bottom:0.35rem;">🪙 CoinClick</h2>
            <p style="color:rgba(255,255,255,0.72); font-size:0.9rem; margin-bottom:0.75rem;">
                Click GoodDollar logo coins. Avoid bombs. 10 plays/day, 50 G$ daily earn cap.
            </p>
            <div style="display:flex; justify-content:center; gap:0.75rem; flex-wrap:wrap; margin-bottom:0.75rem;">
                <span style="background:rgba(251,191,36,0.14); border:1px solid rgba(251,191,36,0.35); padding:0.45rem 0.8rem; border-radius:999px;">Score: <b id="coinClickScore">0</b> G$</span>
                <span style="background:rgba(99,102,241,0.14); border:1px solid rgba(99,102,241,0.35); padding:0.45rem 0.8rem; border-radius:999px;">Time: <b id="coinClickTime">${duration}</b>s</span>
                <span style="background:rgba(16,185,129,0.14); border:1px solid rgba(16,185,129,0.35); padding:0.45rem 0.8rem; border-radius:999px;">Plays left: <b>${limitCheck.remaining_plays}</b></span>
            </div>
            <canvas id="coinClickCanvas" width="720" height="420" style="width:100%; max-width:720px; background:linear-gradient(#172554 0%, #334155 52%, #1e293b 53%, #0f172a 100%); border:2px solid #fbbf24; border-radius:16px; cursor:pointer;"></canvas>
            <div id="coinClickResult" style="display:none; margin-top:0.9rem;"></div>
        </div>`;
    modal.style.display = 'flex';
    runCoinClickGame(sessionId, config, duration);
}

function runCoinClickGame(sessionId, config, duration) {
    const canvas = document.getElementById('coinClickCanvas');
    const ctx = canvas.getContext('2d');
    const scoreEl = document.getElementById('coinClickScore');
    const timeEl = document.getElementById('coinClickTime');
    const items = [];
    const state = {
        sessionId,
        score: 0,
        coinsClicked: 0,
        bombsHit: 0,
        running: true,
        startTime: performance.now(),
        lastSpawn: 0,
        durationMs: duration * 1000,
        animationId: null,
    };
    coinClickState = state;

    function spawnItem(now) {
        if (now - state.lastSpawn < 520) return;
        state.lastSpawn = now;
        const isBomb = Math.random() < 0.22;
        const values = config.coin_values || [1, 2, 5];
        items.push({
            x: 35 + Math.random() * (canvas.width - 70),
            y: -30,
            r: isBomb ? 24 : 26,
            speed: 1.7 + Math.random() * 2.4,
            kind: isBomb ? 'bomb' : 'coin',
            value: values[Math.floor(Math.random() * values.length)],
            spin: Math.random() * Math.PI,
        });
    }

    function drawGoodDollarCoin(item) {
        const gradient = ctx.createRadialGradient(item.x - 8, item.y - 8, 4, item.x, item.y, item.r);
        gradient.addColorStop(0, '#e0f2fe');
        gradient.addColorStop(0.45, '#0284c7');
        gradient.addColorStop(1, '#075985');
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(item.x, item.y, item.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.lineWidth = 5;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 19px Inter, Arial';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('G$', item.x, item.y + 1);
        ctx.fillStyle = '#fbbf24';
        ctx.font = 'bold 12px Inter, Arial';
        ctx.fillText(`+${item.value}`, item.x, item.y + item.r + 13);
    }

    function drawBomb(item) {
        ctx.fillStyle = '#111827';
        ctx.beginPath();
        ctx.arc(item.x, item.y, item.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = 4;
        ctx.stroke();
        ctx.fillStyle = '#f97316';
        ctx.font = '22px Arial';
        ctx.fillText('💣', item.x, item.y + 1);
    }

    async function finishGame() {
        state.running = false;
        cancelAnimationFrame(state.animationId);
        canvas.style.cursor = 'default';
        try {
            const res = await fetch('/minigames/api/complete-game', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    score: state.score,
                    game_data: {
                        clicked_value: state.score,
                        coins_clicked: state.coinsClicked,
                        bombs_hit: state.bombsHit
                    }
                })
            });
            const data = await res.json();
            const resultEl = document.getElementById('coinClickResult');
            resultEl.style.display = 'block';
            if (data.success) {
                resultEl.innerHTML = `<div style="background:rgba(16,185,129,0.14); border:1px solid rgba(16,185,129,0.35); border-radius:14px; padding:1rem; color:#d1fae5;">✅ ${data.message}<br><small>Coins clicked: ${state.coinsClicked} · Bombs hit: ${state.bombsHit} · Plays left: ${data.remaining_plays}</small></div>`;
                showNotification(`CoinClick complete: +${Number(data.winnings || 0).toFixed(2)} G$`, 'success');
                if (typeof loadBalance === 'function') await loadBalance();
            } else {
                resultEl.innerHTML = `<div style="background:rgba(239,68,68,0.14); border:1px solid rgba(239,68,68,0.35); border-radius:14px; padding:1rem; color:#fecaca;">❌ ${data.error || 'Reward save failed.'}</div>`;
            }
        } catch (error) {
            console.error('CoinClick complete error:', error);
            showNotification('Network error while saving CoinClick reward.', 'error');
        }
    }

    canvas.onclick = (event) => {
        if (!state.running) return;
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const x = (event.clientX - rect.left) * scaleX;
        const y = (event.clientY - rect.top) * scaleY;
        for (let i = items.length - 1; i >= 0; i--) {
            const item = items[i];
            if (Math.hypot(item.x - x, item.y - y) <= item.r + 8) {
                if (item.kind === 'bomb') {
                    state.bombsHit += 1;
                    state.score = Math.max(0, state.score - (config.bomb_penalty || 2));
                } else {
                    state.coinsClicked += 1;
                    state.score += item.value;
                }
                items.splice(i, 1);
                scoreEl.textContent = state.score;
                break;
            }
        }
    };

    function loop(now) {
        if (!state.running) return;
        const elapsed = now - state.startTime;
        const remaining = Math.max(0, Math.ceil((state.durationMs - elapsed) / 1000));
        timeEl.textContent = remaining;
        if (elapsed >= state.durationMs) {
            finishGame();
            return;
        }

        spawnItem(now);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = 'rgba(255,255,255,0.07)';
        for (let i = 0; i < 70; i++) ctx.fillRect((i * 97) % canvas.width, (i * 53) % canvas.height, 2, 2);
        for (let i = items.length - 1; i >= 0; i--) {
            const item = items[i];
            item.y += item.speed;
            item.spin += 0.08;
            if (item.y > canvas.height + 40) {
                items.splice(i, 1);
                continue;
            }
            if (item.kind === 'bomb') drawBomb(item); else drawGoodDollarCoin(item);
        }
        state.animationId = requestAnimationFrame(loop);
    }

    state.animationId = requestAnimationFrame(loop);
}

const originalCloseGameModal = window.closeGameModal;
window.closeGameModal = function() {
    if (coinClickState?.animationId) {
        coinClickState.running = false;
        cancelAnimationFrame(coinClickState.animationId);
    }
    coinClickState = null;
    originalCloseGameModal();
};
