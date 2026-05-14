module.exports = {
  apps: [
    {
      name: "finbot",
      // Define o diretório de trabalho para que o PM2 encontre os arquivos e o venv
      cwd: "c:/Users/gilbe/Projects/finbot",
      // Aponta para o interpretador Python dentro do seu ambiente virtual
      script: "./.venv/Scripts/python.exe", 
      // Executa o uvicorn como um módulo Python e corrige o caminho da aplicação
      args: "-m uvicorn main:app --host 0.0.0.0 --port 8000",
      autorestart: true,
      env: {
        PYTHONPATH: ".", // Garante que o diretório atual seja pesquisado por módulos
      }
    },
  ],
};