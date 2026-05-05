from flask import Blueprint, jsonify, session
from maintenance_service import maintenance_service
from .manager import daily_checkin_manager

daily_checkin_bp = Blueprint("daily_checkin", __name__, url_prefix="/daily-checkin")


def _auth_wallet():
    wallet = session.get("wallet") or session.get("wallet_address")
    verified = session.get("verified") or session.get("ubi_verified")
    if not wallet or not verified:
        return None
    return wallet


@daily_checkin_bp.route('/api/status')
def status():
    m = maintenance_service.get_maintenance_status('daily_checkin')
    if m.get('is_maintenance'):
        wallet = _auth_wallet()
        if not wallet:
            return jsonify({'success': False, 'maintenance': True, 'message': m.get('message'), 'error': 'Verification required'}), 401
        status = daily_checkin_manager.get_status(wallet)
        status.update({'maintenance': True, 'message': m.get('message'), 'maintenance_exempt_checkin_available': status.get('can_checkin', False)})
        return jsonify(status), 200
    wallet = _auth_wallet()
    if not wallet:
        return jsonify({'success': False, 'error': 'Verification required'}), 401
    return jsonify(daily_checkin_manager.get_status(wallet))


@daily_checkin_bp.route('/api/checkin', methods=['POST'])
def checkin():
    m = maintenance_service.get_maintenance_status('daily_checkin')
    if m.get('is_maintenance'):
        wallet = _auth_wallet()
        if not wallet:
            return jsonify({'success': False, 'maintenance': True, 'message': m.get('message'), 'error': 'Verification required'}), 401
        result = daily_checkin_manager.maintenance_exempt_checkin(wallet)
        result.update({'maintenance': True, 'message': m.get('message')})
        return jsonify(result), (200 if result.get('success') else 400)
    wallet = _auth_wallet()
    if not wallet:
        return jsonify({'success': False, 'error': 'Verification required'}), 401
    result = daily_checkin_manager.checkin(wallet)
    return jsonify(result), (200 if result.get('success') else 400)


@daily_checkin_bp.route('/api/withdraw-weekly', methods=['POST'])
def withdraw_weekly():
    m = maintenance_service.get_maintenance_status('daily_checkin')
    if m.get('is_maintenance'):
        return jsonify({'success': False, 'maintenance': True, 'message': m.get('message')}), 503
    wallet = _auth_wallet()
    if not wallet:
        return jsonify({'success': False, 'error': 'Verification required'}), 401
    result = daily_checkin_manager.withdraw_weekly_bonus(wallet)
    return jsonify(result), (200 if result.get('success') else 400)


@daily_checkin_bp.route('/api/history')
def history():
    wallet = _auth_wallet()
    if not wallet:
        return jsonify({'success': False, 'error': 'Verification required'}), 401
    return jsonify(daily_checkin_manager.history(wallet, 20))
