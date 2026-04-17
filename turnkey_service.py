import os
import json
import logging
import urllib.request
import urllib.error
from web3 import Web3

logger = logging.getLogger(__name__)

_vercel_runtime_url = os.getenv('VERCEL_PROJECT_PRODUCTION_URL') or os.getenv('VERCEL_URL')
if _vercel_runtime_url and not _vercel_runtime_url.startswith('http'):
    _vercel_runtime_url = f"https://{_vercel_runtime_url}"

WC_SERVICE_URL = os.getenv('WC_SERVICE_URL') or (_vercel_runtime_url.rstrip('/') + '/api/wc' if _vercel_runtime_url else 'http://127.0.0.1:3001')
CELO_RPC = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CELO_CHAIN_ID = 42220


def _call_wc(method, path, body=None, timeout=30):
    url = f"{WC_SERVICE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method=method
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        try:
            err_data = json.loads(err_body)
            return None, err_data.get('error', err_body)
        except Exception:
            return None, err_body
    except urllib.error.URLError as e:
        return None, f"Turnkey service unavailable: {e.reason}"
    except Exception as e:
        return None, str(e)


def create_turnkey_wallet(user_id, user_name=None):
    """Create a new Turnkey sub-org with a fresh Celo wallet for a user."""
    data, err = _call_wc('POST', '/turnkey/create-wallet', {
        'userId': str(user_id),
        'userName': user_name or str(user_id)
    }, timeout=60)
    if err:
        logger.error(f"Turnkey create-wallet error: {err}")
        return None, err
    return data, None


def send_email_otp_turnkey(email):
    """Ask Turnkey sidecar to send an email OTP code."""
    payload = {"email": str(email).strip().lower()}
    candidate_paths = [
        "/turnkey/email/send-code",
        "/turnkey/email/send-otp",
        "/turnkey/otp/send",
    ]
    last_err = None
    for path in candidate_paths:
        data, err = _call_wc("POST", path, payload, timeout=30)
        if err:
            last_err = err
            continue
        # accept common response shapes
        if isinstance(data, dict) and data.get("success") is False:
            last_err = data.get("error") or data.get("message") or "Turnkey OTP send failed"
            continue
        return data or {"success": True}, None
    return None, last_err or "Turnkey email OTP send unavailable"


def verify_email_otp_turnkey(email, code):
    """Ask Turnkey sidecar to verify an email OTP code."""
    payload = {
        "email": str(email).strip().lower(),
        "code": str(code).strip(),
    }
    candidate_paths = [
        "/turnkey/email/verify-code",
        "/turnkey/email/verify-otp",
        "/turnkey/otp/verify",
    ]
    last_err = None
    for path in candidate_paths:
        data, err = _call_wc("POST", path, payload, timeout=30)
        if err:
            last_err = err
            continue
        if isinstance(data, dict) and data.get("success") is False:
            last_err = data.get("error") or data.get("message") or "Invalid OTP code"
            continue
        return data or {"success": True}, None
    return None, last_err or "Turnkey email OTP verify unavailable"


def import_private_key(user_id, private_key, user_name=None):
    """Import an existing private key into a new Turnkey sub-org for a user."""
    data, err = _call_wc('POST', '/turnkey/import-key', {
        'userId': str(user_id),
        'userName': user_name or str(user_id),
        'privateKey': private_key
    }, timeout=120)
    if err:
        logger.error(f"Turnkey import-key error: {err}")
        return None, err
    return data, None


def get_wallet_info(suborg_id):
    """Get the wallet address and signWith value for a Turnkey sub-org."""
    data, err = _call_wc('GET', f'/turnkey/wallet/{suborg_id}', timeout=15)
    if err:
        logger.error(f"Turnkey wallet info error: {err}")
        return None, err
    return data, None


def sign_transaction_turnkey(suborg_id, sign_with, unsigned_tx_hex):
    """Sign a raw EVM transaction using Turnkey and return the signed hex."""
    data, err = _call_wc('POST', '/turnkey/sign-tx', {
        'subOrgId': suborg_id,
        'signWith': sign_with,
        'unsignedTx': unsigned_tx_hex
    }, timeout=30)
    if err:
        logger.error(f"Turnkey sign-tx error: {err}")
        return None, err
    return data.get('signedTx'), None


def sign_message_turnkey(suborg_id, sign_with, message):
    """Sign a personal message using Turnkey."""
    data, err = _call_wc('POST', '/turnkey/sign-msg', {
        'subOrgId': suborg_id,
        'signWith': sign_with,
        'message': message
    }, timeout=30)
    if err:
        logger.error(f"Turnkey sign-msg error: {err}")
        return None, err
    return data.get('signature'), None


def export_wallet_account_turnkey(suborg_id, address):
    """Export + decrypt a Turnkey wallet account private key via sidecar."""
    payload = {
        "subOrgId": str(suborg_id),
        "address": str(address),
    }
    candidate_paths = [
        "/turnkey/export-wallet-account",
        "/turnkey/export-private-key",
    ]
    last_err = None
    for path in candidate_paths:
        data, err = _call_wc("POST", path, payload, timeout=60)
        if err:
            last_err = err
            continue
        if isinstance(data, dict) and data.get("success") is False:
            last_err = data.get("error") or data.get("message") or "Turnkey export failed"
            continue
        private_key = (data or {}).get("privateKey") or (data or {}).get("private_key")
        if private_key:
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            return private_key, None
        last_err = "No private key returned from Turnkey export"
    return None, last_err or "Turnkey export unavailable"


def build_and_sign_erc20_transfer(suborg_id, sign_with, from_address,
                                   to_address, amount_wei, contract_address,
                                   nonce=None, gas=None, gas_price=None):
    """Build an ERC20 transfer tx, sign via Turnkey, and return signed hex."""
    w3 = Web3(Web3.HTTPProvider(CELO_RPC))

    erc20_abi = [{
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    }]

    checksum_from = Web3.to_checksum_address(from_address)
    checksum_to = Web3.to_checksum_address(to_address)
    checksum_contract = Web3.to_checksum_address(contract_address)

    contract = w3.eth.contract(address=checksum_contract, abi=erc20_abi)

    if nonce is None:
        nonce = w3.eth.get_transaction_count(checksum_from)
    if gas_price is None:
        gas_price = w3.eth.gas_price
    if gas is None:
        gas = 100000

    tx = contract.functions.transfer(checksum_to, amount_wei).build_transaction({
        'chainId': CELO_CHAIN_ID,
        'from': checksum_from,
        'nonce': nonce,
        'gas': gas,
        'gasPrice': gas_price
    })

    unsigned_hex = w3.eth.account.encode_defunct(
        transaction=None
    )

    raw_tx = {
        'nonce': tx['nonce'],
        'gasPrice': tx['gasPrice'],
        'gas': tx['gas'],
        'to': tx['to'],
        'value': tx.get('value', 0),
        'data': tx.get('data', b''),
        'chainId': CELO_CHAIN_ID,
    }

    from eth_account._utils.legacy_transactions import encode_transaction, serializable_unsigned_transaction_from_dict
    from eth_account._utils.typed_transactions import TypedTransaction

    unsigned_tx = serializable_unsigned_transaction_from_dict(raw_tx)
    import rlp
    unsigned_bytes = rlp.encode(unsigned_tx)
    unsigned_hex_str = '0x' + unsigned_bytes.hex()

    signed_hex, err = sign_transaction_turnkey(suborg_id, sign_with, unsigned_hex_str)
    if err:
        return None, None, err

    tx_hash = w3.eth.send_raw_transaction(signed_hex)
    return tx_hash.hex(), signed_hex, None


def broadcast_signed_tx(signed_tx_hex):
    """Broadcast a signed transaction to the Celo network."""
    w3 = Web3(Web3.HTTPProvider(CELO_RPC))
    try:
        tx_hash = w3.eth.send_raw_transaction(signed_tx_hex)
        return tx_hash.hex(), None
    except Exception as e:
        return None, str(e)
