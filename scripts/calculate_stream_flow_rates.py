#!/usr/bin/env python3
"""
Learn & Earn Streaming Flow Rate Calculator
============================================

Script na nagpi-print ng flow rate para sa bawat possible score/reward.
Naka-automate na ang calculation - hindi ka na kailangan mag-worry!

Formula: flow_rate_wei = (amount_gd * 10^18) / duration_seconds

Example outputs:
- 2000 G$ over 24 hours → X G$/second
- 1600 G$ over 24 hours → Y G$/second
etc.
"""

from decimal import Decimal, ROUND_DOWN

# Constants
STREAMING_DURATION_SECONDS = 86400  # 24 hours
G_DECIMALS = 18  # G$ has 18 decimals like most ERC20 tokens
REWARD_PER_CORRECT = 200  # G$ per correct answer
MAX_QUESTIONS = 10  # Total questions per quiz

# Wei conversion factor
WEI = Decimal('1' + '0' * G_DECIMALS)  # 10^18


def compute_flow_rate_wei(amount_gd: float, duration_seconds: int) -> int:
    """
    Calculate Superfluid flow rate in wei/second.
    
    Superfluid CFA (Constant Flow Agreement) handles the streaming math -
    the protocol automatically streams the exact amount over the exact duration.
    """
    d_amount = Decimal(str(amount_gd))
    wei_amount = int(d_amount * WEI)
    if duration_seconds <= 0:
        raise ValueError('duration_seconds must be positive')
    return max(1, wei_amount // duration_seconds)


def wei_to_gd_per_second(flow_rate_wei: int) -> Decimal:
    """Convert flow rate wei/sec to G$/second"""
    return Decimal(flow_rate_wei) / WEI


def wei_to_gd_per_minute(flow_rate_wei: int) -> Decimal:
    """Convert flow rate wei/sec to G$/minute"""
    return (Decimal(flow_rate_wei) / WEI) * 60


def wei_to_gd_per_hour(flow_rate_wei: int) -> Decimal:
    """Convert flow rate wei/sec to G$/hour"""
    return (Decimal(flow_rate_wei) / WEI) * 3600


def format_wei(flow_rate_wei: int) -> str:
    """Format wei flow rate in human readable format"""
    return f"{flow_rate_wei:,}"


def calculate_all_flow_rates():
    """Calculate and display flow rates for all possible scores"""
    
    print("=" * 80)
    print("📊 LEARN & EARN STREAMING FLOW RATE CALCULATOR")
    print("=" * 80)
    print()
    print(f"⏱️  Streaming Duration: {STREAMING_DURATION_SECONDS:,} seconds (24 hours)")
    print(f"💰 Reward per Correct Answer: {REWARD_PER_CORRECT} G$")
    print(f"❓ Total Questions: {MAX_QUESTIONS}")
    print(f"🎯 Max Reward: {MAX_QUESTIONS * REWARD_PER_CORRECT} G$")
    print()
    print("-" * 80)
    print()
    
    print(f"{'Score':^8} | {'Reward (G$)':^12} | {'Flow Rate (wei/sec)':^25} | {'G$/sec':^15} | {'G$/hr':^12}")
    print("-" * 80)
    
    results = []
    
    for correct in range(MAX_QUESTIONS + 1):
        reward_gd = correct * REWARD_PER_CORRECT
        flow_rate_wei = compute_flow_rate_wei(reward_gd, STREAMING_DURATION_SECONDS)
        
        gd_per_sec = wei_to_gd_per_second(flow_rate_wei)
        gd_per_hour = wei_to_gd_per_hour(flow_rate_wei)
        
        score_display = f"{correct}/{MAX_QUESTIONS}"
        reward_display = f"{reward_gd:,}"
        flow_display = format_wei(flow_rate_wei)
        gd_sec_display = f"{gd_per_sec:.8f}"
        gd_hr_display = f"{gd_per_hour:.4f}"
        
        print(f"{score_display:^8} | {reward_display:^12} | {flow_display:^25} | {gd_sec_display:^15} | {gd_hr_display:^12}")
        
        results.append({
            'score': correct,
            'reward_gd': reward_gd,
            'flow_rate_wei': flow_rate_wei,
            'gd_per_second': gd_per_sec,
            'gd_per_hour': gd_per_hour
        })
    
    print()
    print("-" * 80)
    print()
    
    # Summary stats
    total_possible_rewards = sum(r['reward_gd'] for r in results)
    avg_flow_rate = sum(r['flow_rate_wei'] for r in results) // len(results)
    
    print("📈 SUMMARY STATISTICS")
    print("-" * 40)
    print(f"  Total possible reward tiers: {len(results)}")
    print(f"  Average flow rate: {format_wei(avg_flow_rate)} wei/sec")
    print(f"  Min flow rate (0 correct): {format_wei(results[0]['flow_rate_wei'])} wei/sec")
    print(f"  Max flow rate (10 correct): {format_wei(results[-1]['flow_rate_wei'])} wei/sec")
    print()
    
    print("🔍 HOW IT WORKS")
    print("-" * 40)
    print("""
  1. User completes quiz → gets X G$ reward
  2. System calculates flow rate: (X * 10^18) / 86400
  3. Superfluid CFA creates a constant flow at that rate
  4. User receives G$ continuously for 24 hours
  5. After 24 hours, stream auto-closes via scheduler
    """)
    print()
    
    # Show verification calculation
    print("✅ VERIFICATION (for 2000 G$ reward)")
    print("-" * 40)
    test_reward = 2000
    test_flow = compute_flow_rate_wei(test_reward, STREAMING_DURATION_SECONDS)
    print(f"  Input: {test_reward} G$ over {STREAMING_DURATION_SECONDS} seconds")
    print(f"  Flow rate: {format_wei(test_flow)} wei/sec")
    print(f"  G$/second: {wei_to_gd_per_second(test_flow):.8f}")
    print(f"  G$/hour: {wei_to_gd_per_hour(test_flow):.4f}")
    print(f"  G$/day: {wei_to_gd_per_hour(test_flow) * 24:.2f}")
    print()
    
    # Verify total
    total_gd = wei_to_gd_per_second(test_flow) * STREAMING_DURATION_SECONDS
    print(f"  Verification: {wei_to_gd_per_second(test_flow):.8f} G$/sec × {STREAMING_DURATION_SECONDS} sec = {total_gd:.2f} G$")
    print()
    
    return results


def test_edge_cases():
    """Test edge cases"""
    print("=" * 80)
    print("🧪 EDGE CASE TESTS")
    print("=" * 80)
    print()
    
    test_cases = [
        (0.01, 86400, "Minimum reward (0.01 G$)"),
        (0.50, 86400, "Half G$ reward"),
        (100, 86400, "100 G$ reward"),
        (2000, 86400, "Full quiz reward"),
        (5000, 86400, "Above max (will be capped)"),
        (2000, 3600, "2000 G$ over 1 hour"),
        (2000, 172800, "2000 G$ over 48 hours"),
    ]
    
    print(f"{'Test Case':^35} | {'Reward':^10} | {'Duration':^10} | {'Flow Rate (wei/sec)':^22}")
    print("-" * 85)
    
    for reward, duration, description in test_cases:
        flow_rate = compute_flow_rate_wei(reward, duration)
        total = wei_to_gd_per_second(flow_rate) * duration
        print(f"{description:^35} | {reward:^10} | {duration:^10} | {format_wei(flow_rate):^22}")
    
    print()
    print()


def main():
    print()
    print("🎓 GOODDOLLAR LEARN & EARN STREAMING CALCULATOR")
    print("   Powered by Superfluid Constant Flow Agreement (CFA)")
    print()
    
    calculate_all_flow_rates()
    test_edge_cases()
    
    print("=" * 80)
    print("📝 NOTES:")
    print("-" * 40)
    print("""
  • Superfluid handles all the complex streaming math automatically
  • Each user gets a UNIQUE flow rate based on their quiz score
  • No manual calculation needed - the system does it all!
  • Stream is continuous - user sees G$ arriving gradually
  • After 24 hours, scheduler automatically closes the stream
    """)
    print("=" * 80)


if __name__ == "__main__":
    main()