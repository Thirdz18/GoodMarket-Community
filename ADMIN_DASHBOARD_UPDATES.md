# Admin Dashboard - Referral Program Updates

## What Was Added

I've enhanced the admin dashboard's referral section with **real-time REFERRAL_KEY balance monitoring** to address the issues we found earlier.

---

## New Features in Admin Dashboard

### 1. **REFERRAL_KEY Wallet Status Card**

**Location:** Top of Referral Program section
**Shows:**
- Current G$ balance in REFERRAL_KEY wallet
- Status indicator (✅ Sufficient, ⚠️ Medium, ⚠️ Low)
- Pending disbursements count
- Total G$ owed to pending users
- Can Process Now? (Yes/No indicator)

**Colors:**
- 🟢 Green (≥2000 G$): Safe, no concerns
- 🟡 Yellow (500-1999 G$): Medium, monitor closely
- 🔴 Red (<500 G$): Critical, cannot process disbursements

### 2. **Smart Alert System**

When balance is insufficient:
- Automatic alert appears below balance card
- Shows exactly how much more G$ is needed
- Prompts admin to top up the wallet

**Example Alert:**
```
⚠️ Low Balance Alert: REFERRAL_KEY needs at least 5,234.50 more G$ 
to process pending disbursements. Please top up the wallet.
```

### 3. **Refresh Button**

Admin can manually refresh the balance anytime without reloading page.

---

## How Admins Use It

### Daily Workflow:

1. **Open Admin Dashboard** → Click "Referral Program" in sidebar
2. **Check Balance Card** at the top
   - If green (✅ Sufficient): System is healthy, continue monitoring
   - If yellow (⚠️ Medium): Keep an eye on it, might need topup soon
   - If red (❌ Low): Top up immediately
3. **If topup is needed:**
   - Go to Unified Treasury section (below)
   - Transfer G$ from treasury to REFERRAL_KEY wallet
4. **Click "Refresh Balance"** after topup
5. **Click "Process Pending Rewards"** if pending count > 0

---

## Technical Details

### New Admin Endpoint Used

```
GET /api/admin/referral/key-balance
```

**Returns:**
```json
{
  "success": true,
  "balance_g": 1500.25,           // Current G$ balance
  "balance_wei": "1500250000000000000000",  // Wei format
  "wallet": "0x1234...abcd",      // Wallet address
  "pending_disbursements_count": 3,  // Stuck rewards
  "total_pending_amount_g": 234.50,  // G$ owed
  "can_process": false            // Can retry now?
}
```

### JavaScript Function

```javascript
loadReferralKeyBalance()
```

Automatically called when:
- Admin opens Referral Program section
- Admin clicks "Refresh Balance" button

---

## What This Solves

### Before (without balance monitoring):
1. Admin topples up REFERRAL_KEY
2. Admin has NO WAY to see if there's enough balance
3. Might call "Process Pending" when balance is still insufficient
4. Confused about why it's still not working

### After (with balance monitoring):
1. Admin sees exactly how much G$ is in the wallet
2. Admin sees how much is pending
3. Admin gets automatic alert if insufficient
4. Admin can see "Can Process Now?" indicator before trying
5. Admin knows exactly what to do next

---

## Status Meanings

**Pending Disbursements Count:**
- Shows number of rewards waiting for balance
- These are marked as `'pending_disbursed'` in database
- Will automatically retry when balance is topped up

**Can Process Now?**
- ✅ Yes: Balance ≥ pending amount, safe to click "Process Pending"
- ❌ No: Balance < pending amount, need to top up first

---

## Integration with Other Sections

The balance card works seamlessly with:

1. **Unified Treasury Section**
   - View all wallet recipients
   - Transfer G$ to REFERRAL_KEY directly
   - No need to switch pages

2. **Process Pending Rewards Button**
   - Batch retry all stuck disbursements
   - Works better when balance is sufficient
   - Shows result/error after processing

3. **Referral Stats Cards**
   - Shows how many are pending_disbursed
   - Shows failed disbursements separately
   - Gives full picture of system health

---

## Code Changes Summary

### File Modified: templates/admin_dashboard.html

**Changes:**
1. Added new "REFERRAL_KEY Wallet Status" card section (lines ~2351)
2. Added `loadReferralKeyBalance()` JavaScript function (lines ~6273)
3. Updated section load to call balance function (lines ~2740)

**No database migrations needed**
**No backend code changes needed** (uses existing endpoint)

---

## Monitoring Recommendations

### Daily
- Check balance card color
- Alert if red (balance < 500)
- Alert if pending count > 0

### Weekly
- Review pending disbursements trend
- Check if same users are appearing repeatedly
- Verify balance is stable

### Monthly
- Review total G$ distributed
- Compare against budget
- Plan capacity needs

---

## Troubleshooting

### Balance Not Loading?
- Check browser console for errors
- Verify admin is logged in
- Refresh page and try again
- Check if /api/admin/referral/key-balance endpoint exists

### Pending Count Wrong?
- Click "Refresh Balance" button
- Check database directly: 
  ```sql
  SELECT COUNT(*) FROM referral_rewards_log 
  WHERE status = 'pending_disbursed';
  ```

### Process Pending Still Fails?
- Check balance first with balance card
- Ensure balance > pending_amount
- Check backend logs for errors
- Manually verify REFERRAL_KEY wallet has funds

---

## Next Steps

1. **Deploy Code Changes**
   - Only templates/admin_dashboard.html modified
   - No backend changes
   - No database migrations
   - Safe to deploy immediately

2. **Test in Staging**
   - Open Referral Program section
   - Verify balance card appears
   - Click Refresh to test API call
   - Check alert appears if balance is low

3. **Deploy to Production**
   - Monitor for first week
   - Train admin team on new feature
   - Update any runbooks/procedures

4. **Optional Enhancements** (future)
   - Auto-refresh balance every 60 seconds
   - Send email/Slack alert if balance drops below threshold
   - Historical balance graph
   - Auto-top-up from treasury (if configured)

---

## Questions & Answers

**Q: Why show both G$ and pending amount?**
A: So admin can quickly calculate if topup is needed: `needed = pending - balance`

**Q: Why the yellow warning zone at 500-2000 G$?**
A: For ~500 users, you need ~500 G$ (1 user per G$). Yellow means you're running low.

**Q: Can this auto-top-up from treasury?**
A: Not yet, but it could be added. For now it's manual for safety.

**Q: What if REFERRAL_KEY wallet address is wrong?**
A: The endpoint will show it. Admin should verify against env vars.

**Q: How often should I check?**
A: Daily is best. The balance and pending count are realtime.

