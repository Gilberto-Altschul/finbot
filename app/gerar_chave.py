from cryptography.fernet import Fernet
# Usa a tua chave que está no .env
key = b'rBAaxAEPC25AmJnD1g9B9091i_4B1QawTIfh3GZynRk=' 
cipher = Fernet(key)
print(cipher.encrypt(b'+5511976582394').decode())