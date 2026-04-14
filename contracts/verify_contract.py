"""
Celoscan API Contract Verification Script

This script verifies the LearnAndEarnRewards contract on Celoscan using their API,
bypassing any UI caching issues.
"""

import os
import json
import time
import requests
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CELOSCAN_API_URL = "https://api.celoscan.io/api"
CELOSCAN_API_KEY = os.getenv('CELOSCAN_API_KEY', '')

CONTRACT_ADDRESS = "0x52347653a24A9A1e432aEC6CD91a271158205963"
COMPILER_VERSION = "v0.8.21+commit.d9974bed"
CONSTRUCTOR_ARGS = "00000000000000000000000062b8b11039fcfe5ab0c56e502b1c372a3d2a9c7a00000000000000000000000000000000000000000000003635c9adc5dea000000000000000000000000000000000000000000000000000000de0b6b3a7640000"


def load_source_code():
    """Load the flattened source code"""
    with open('contracts/LearnAndEarnRewards_Flattened.sol', 'r') as f:
        return f.read()


def load_standard_json():
    """Load the standard JSON input"""
    with open('contracts/standard_json_input.json', 'r') as f:
        return json.load(f)


def verify_with_single_file():
    """Verify using single file method via API"""
    logger.info("Attempting verification with single file method...")
    
    source_code = load_source_code()
    
    params = {
        'apikey': CELOSCAN_API_KEY,
        'module': 'contract',
        'action': 'verifysourcecode',
        'contractaddress': CONTRACT_ADDRESS,
        'sourceCode': source_code,
        'codeformat': 'solidity-single-file',
        'contractname': 'LearnAndEarnRewards',
        'compilerversion': COMPILER_VERSION,
        'optimizationUsed': '1',
        'runs': '200',
        'constructorArguements': CONSTRUCTOR_ARGS,
        'licenseType': '3',  # MIT
    }
    
    response = requests.post(CELOSCAN_API_URL, data=params)
    result = response.json()
    
    logger.info(f"Response: {json.dumps(result, indent=2)}")
    
    return result


def verify_with_standard_json():
    """Verify using standard JSON input method via API"""
    logger.info("Attempting verification with Standard JSON Input method...")
    
    standard_json = load_standard_json()
    
    params = {
        'apikey': CELOSCAN_API_KEY,
        'module': 'contract',
        'action': 'verifysourcecode',
        'contractaddress': CONTRACT_ADDRESS,
        'sourceCode': json.dumps(standard_json),
        'codeformat': 'solidity-standard-json-input',
        'contractname': 'LearnAndEarnRewards.sol:LearnAndEarnRewards',
        'compilerversion': COMPILER_VERSION,
        'constructorArguements': CONSTRUCTOR_ARGS,
    }
    
    response = requests.post(CELOSCAN_API_URL, data=params)
    result = response.json()
    
    logger.info(f"Response: {json.dumps(result, indent=2)}")
    
    if result.get('status') == '1':
        guid = result.get('result')
        logger.info(f"Verification submitted. GUID: {guid}")
        logger.info("Waiting for verification to complete...")
        
        # Check verification status
        for i in range(10):
            time.sleep(5)
            check_params = {
                'apikey': CELOSCAN_API_KEY,
                'module': 'contract',
                'action': 'checkverifystatus',
                'guid': guid,
            }
            check_response = requests.get(CELOSCAN_API_URL, params=check_params)
            check_result = check_response.json()
            logger.info(f"Status check {i+1}: {check_result}")
            
            if check_result.get('result') not in ['Pending in queue', 'In progress...']:
                break
    
    return result


def main():
    logger.info("=" * 60)
    logger.info("Celoscan Contract Verification")
    logger.info("=" * 60)
    logger.info(f"Contract: {CONTRACT_ADDRESS}")
    logger.info(f"Compiler: {COMPILER_VERSION}")
    logger.info("=" * 60)
    
    if not CELOSCAN_API_KEY:
        logger.warning("CELOSCAN_API_KEY not set - API verification may be limited")
        logger.info("\nTo get an API key:")
        logger.info("1. Go to https://celoscan.io/register")
        logger.info("2. Create an account")
        logger.info("3. Go to https://celoscan.io/myapikey")
        logger.info("4. Create a new API key")
        logger.info("5. Set CELOSCAN_API_KEY environment variable")
        logger.info("")
    
    # Try standard JSON method first
    result = verify_with_standard_json()
    
    if result.get('status') != '1':
        logger.info("\nStandard JSON failed, trying single file method...")
        result = verify_with_single_file()
    
    logger.info("\n" + "=" * 60)
    logger.info("Verification attempt complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
