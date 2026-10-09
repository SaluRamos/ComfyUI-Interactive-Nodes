# ComfyUI Interactive Nodes

Node de controle para pausar um workflow e esperar uma escolha sua. Quando a
execução chega ao node, aparece um aviso na interface do ComfyUI com os botões
**Continuar** e **Cancelar**. Os mesmos botões também ficam disponíveis no
node dentro do workflow.

## Instalação

Coloque esta pasta em `ComfyUI/custom_nodes/ComfyUI-Interactive-Nodes` e reinicie o
ComfyUI. Depois atualize a página do navegador para carregar a interface do
node.

## Uso

Insira `Continuar / Cancelar` no ponto do workflow em que deseja aguardar.
Conecte o valor que precisa continuar pelo socket `value` e ligue a saída ao
próximo node. O valor passa pelo node sem conversão.

Quando o node for executado, o aviso aparece com título e descrição
personalizáveis. **Continuar** libera o workflow. **Cancelar** interrompe a
execução atual. A execução também continua respondendo ao botão normal de
interrupção do ComfyUI.

O node não adiciona dependências Python. Os botões e o aviso são fornecidos
pela extensão da interface deste projeto.


## retry

Adicione **retry** em `workflow/control` e conecte sua entrada e saída coringa
`value`. O node possui apenas o botão **retry** e deixa o valor passar sem
conversão. Depois que esse ponto for executado, clicar no botão durante o
workflow retorna a ele e reexecuta os nodes conectados depois dele, usando o
mesmo valor recebido originalmente. Nodes anteriores e ramificações
independentes já concluídas são preservados.

O retorno ocorre entre operações: um node síncrono que já estiver rodando
termina primeiro. Se um node **Continuar / Cancelar** estiver aguardando depois
do retry, essa espera é cancelada e reaparece quando o fluxo chegar nela novamente.
Depois que o fluxo terminar, o botão continua disponível: um clique coloca
uma nova execução na fila normal do ComfyUI, começando nesse retry e usando
os valores preservados. Os nodes anteriores e as entradas externas dos nodes
seguintes não são recalculados. Cliques repetidos enquanto a nova execução
aguarda na fila não criam duplicatas. O fluxo salvo é o da execução anterior;
editar o grafo exige executar normalmente para criar novos pontos de retorno.
Arquivos ou outros efeitos produzidos antes do clique não são desfeitos.

A extensão adapta o executor e o agendador em memória durante a importação,
sem editar os arquivos do ComfyUI. Os valores necessários ficam em memória
após o término para permitir a retomada, inclusive quando o cache está desativado.
Ao executar normalmente outro workflow com retry no mesmo cliente, os pontos
anteriores são substituídos. Reiniciar o ComfyUI elimina esses pontos.
Requer o executor assíncrono da versão instalada do ComfyUI.

Testes (a partir desta pasta, com o Python do ComfyUI):

```powershell
& '..\..\.venv\Scripts\python.exe' tests/test_retry.py
node --check web/retry.js
```
