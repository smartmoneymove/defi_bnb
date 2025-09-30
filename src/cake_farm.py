import os
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
import time

# Загружаем переменные из .env
load_dotenv()

RPC_URL = os.getenv("RPC_URL")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
NONF_POS_MANAGER_ADDRESS = os.getenv("NONF_POS_MANAGER_ADDRESS")  # Адрес NonfungiblePositionManager
FARM_ADDRESS = os.getenv("FARM_ADDRESS")  # Адрес для фарминга

# Подключение к сети
w3 = Web3(Web3.HTTPProvider(RPC_URL))

def get_gas_price(w3: Web3) -> int:
    """Получает актуальную цену газа и добавляет 5% запаса"""
    gas_price = w3.eth.gas_price
    return int(gas_price * 1.05)

def get_nft_position_info(token_id: int) -> dict:
    """Получает информацию о позиции NFT"""
    nonf_pos_manager_address = Web3.to_checksum_address(NONF_POS_MANAGER_ADDRESS)
    
    # ABI для positions
    positions_abi = [
        {
            "inputs": [
                {"internalType": "uint256", "name": "tokenId", "type": "uint256"}
            ],
            "name": "positions",
            "outputs": [
                {"internalType": "uint96", "name": "nonce", "type": "uint96"},
                {"internalType": "address", "name": "operator", "type": "address"},
                {"internalType": "address", "name": "token0", "type": "address"},
                {"internalType": "address", "name": "token1", "type": "address"},
                {"internalType": "uint24", "name": "fee", "type": "uint24"},
                {"internalType": "int24", "name": "tickLower", "type": "int24"},
                {"internalType": "int24", "name": "tickUpper", "type": "int24"},
                {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
                {"internalType": "uint256", "name": "feeGrowthInside0LastX128", "type": "uint256"},
                {"internalType": "uint256", "name": "feeGrowthInside1LastX128", "type": "uint256"},
                {"internalType": "uint128", "name": "tokensOwed0", "type": "uint128"},
                {"internalType": "uint128", "name": "tokensOwed1", "type": "uint128"}
            ],
            "stateMutability": "view",
            "type": "function"
        }
    ]
    
    nft_contract = w3.eth.contract(address=nonf_pos_manager_address, abi=positions_abi)
    
    try:
        position = nft_contract.functions.positions(token_id).call()
        return {
            "nonce": position[0],
            "operator": position[1],
            "token0": position[2],
            "token1": position[3],
            "fee": position[4],
            "tickLower": position[5],
            "tickUpper": position[6],
            "liquidity": position[7],
            "feeGrowthInside0LastX128": position[8],
            "feeGrowthInside1LastX128": position[9],
            "tokensOwed0": position[10],
            "tokensOwed1": position[11]
        }
    except Exception as e:
        print(f"Ошибка при получении информации о позиции NFT: {e}")
        return {}

def stake_nft_in_farm(token_id: int) -> bool:
    """Отправляет NFT в фарминг используя safeTransferFrom"""
    if not FARM_ADDRESS:
        print("Ошибка: Адрес фарминга не указан в .env файле")
        return False

    wallet_address = Web3.to_checksum_address(WALLET_ADDRESS)
    farm_address = Web3.to_checksum_address(FARM_ADDRESS)
    nonf_pos_manager_address = Web3.to_checksum_address(NONF_POS_MANAGER_ADDRESS)
    
    # ABI для safeTransferFrom (ERC-721)
    # Метод safeTransferFrom с сигнатурой 0x42842e0e
    nft_transfer_abi = [
        {
            "inputs": [
                {"internalType": "address", "name": "from", "type": "address"},
                {"internalType": "address", "name": "to", "type": "address"},
                {"internalType": "uint256", "name": "tokenId", "type": "uint256"}
            ],
            "name": "safeTransferFrom",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function"
        }
    ]
    
    nft_contract = w3.eth.contract(address=nonf_pos_manager_address, abi=nft_transfer_abi)
    
    print(f"Отправляем NFT с ID {token_id} в фарминг используя safeTransferFrom...")
    print(f"Адрес фарминга: {farm_address}")
    
    try:
        # Создаем транзакцию safeTransferFrom
        transfer_tx = nft_contract.functions.safeTransferFrom(
            wallet_address,  # от кого
            farm_address,    # кому
            token_id         # ID токена
        ).build_transaction({
            "from": wallet_address,
            "nonce": w3.eth.get_transaction_count(wallet_address),
            "gas": 800000,   # Увеличиваем лимит газа для безопасности
            "gasPrice": get_gas_price(w3)
        })
        
        # Подписываем и отправляем транзакцию
        signed_tx = w3.eth.account.sign_transaction(transfer_tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        print(f"Транзакция отправлена: {tx_hash.hex()}")
        
        # Ждем подтверждения транзакции
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            print(f"NFT успешно отправлен в фарминг. Tx: {tx_hash.hex()}")
            return True
        else:
            print(f"Ошибка при отправке NFT в фарминг. Tx: {tx_hash.hex()}")
            return False
            
    except Exception as e:
        print(f"Произошла ошибка при отправке NFT в фарминг: {str(e)}")
        return False

if __name__ == "__main__":
    try:
        token_id = int(input("Введите ID токена для отправки в фарминг: "))
        if token_id <= 0:
            print("Ошибка: ID токена должен быть положительным числом")
            exit(1)
        
        # Получаем информацию о позиции NFT
        position_info = get_nft_position_info(token_id)
        if position_info:
            print(f"\nИнформация о позиции NFT {token_id}:")
            print(f"Токен 0: {position_info['token0']}")
            print(f"Токен 1: {position_info['token1']}")
            print(f"Комиссия: {position_info['fee']}")
            print(f"Ликвидность: {position_info['liquidity']}")
            
        # Отправляем NFT в фарминг (без предварительного одобрения)
        if stake_nft_in_farm(token_id):
            print("Операция успешно завершена!")
        else:
            print("Операция не удалась.")
            
    except ValueError:
        print("Ошибка: Введите корректный ID токена (целое число)")
        exit(1)
    except KeyboardInterrupt:
        print("\nОперация отменена пользователем")
        exit(0) 
