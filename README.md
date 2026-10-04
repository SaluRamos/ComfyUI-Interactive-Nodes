# ComfyUI Proceed Button

Node de controle para pausar um workflow e esperar uma escolha sua. Quando a
execução chega ao node, aparece um aviso na interface do ComfyUI com os botões
**Continuar** e **Cancelar**. Os mesmos botões também ficam disponíveis no
node dentro do workflow.

## Instalação

Coloque esta pasta em `ComfyUI/custom_nodes/ComfyUI-proceed-btn` e reinicie o
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
