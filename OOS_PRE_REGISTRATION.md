# Pré-Registo Oficial do OOS Forex — FOLOMA Visão 360

**Data de Congelamento:** 22 de Outubro de 2026
**Hash do Commit (Código em Produção):** `v_oos_1`
**Data de Início Oficial:** O momento exato do primeiro evento válido gravado na tabela `forex_oos_events` após as correções de 22/10/2026.

## 1. Definição de Episódio e Evento
*   **Episódio:** Um episódio é definido por um par e uma direção (ex: EURUSD_BUY). Ele inicia quando o sinal é gerado e termina quando há um intervalo de **1000 segundos** sem que a mesma direção seja confirmada.
*   **Evento:** Cada episódio pode gerar até 3 eventos, correspondentes aos limiares de confiança de **60%, 70% e 80%**.
*   O evento é registado no instante exato (`ts_cross`) em que a confiança atinge ou supera o limiar pela primeira vez no episódio.
*   **Validade:** Se a idade do último tick no momento do cruzamento for superior a **60 segundos** (indicando mercado fechado ou parado), o evento é descartado.

## 2. Regra de Avaliação de Resultado
*   **Preço de Entrada:** O último tick no momento do `ts_cross`.
*   **Preço de Saída:** O primeiro tick disponível com timestamp `>= ts_cross + 900` (15 minutos exatos).
*   **Falha de Dados (Void):** Se o atraso para obter o tick de saída for superior a 60 segundos, ou se não houver tick, o evento é marcado como `void` e não conta para o Win Rate.
*   **Empates:** Se o preço de saída for exatamente igual ao preço de entrada, conta como **Loss** (regra padrão de Rise/Fall da Deriv).
*   **Integridade:** O evento é avaliado uma única vez pelo job automático e não pode ser reescrito.

## 3. Regra de Decisão Estatística
*   **Breakeven:** `1 / (1 + payout)`. O payout real do contrato de 15 minutos será registado no momento do evento.
*   **Paragem por Futilidade:** Com `n >= 300` eventos avaliados no limiar de 80%, se o limite superior do Intervalo de Confiança (95% Wilson) do Win Rate ficar **abaixo** do breakeven, o Forex encerra oficialmente (sem edge).
*   **Análise Final:** Com `n >= 1000` eventos avaliados no limiar de 80%, se o limite inferior do IC95% ficar **acima** do breakeven em ambas as amostras (todos os eventos e 1 evento/hora/pair), o Forex tem edge.
*   **Alterações:** Nenhum limiar, indicador ou filtro será alterado durante a medição. Qualquer alteração no código reinicia a contagem do OOS do zero.

## 4. Amostragem e Independência
*   **Amostra A (Todos):** Todos os eventos válidos.
*   **Amostra B (Independente):** Máximo de 1 evento por limiar, por par, por hora UTC (para mitigar a correlação entre pares como EURUSD e GBPUSD).
*   A conclusão final tem de ser positiva em ambas as amostras.

## 5. Regra de Dinheiro Real
*   Nenhum capital real será usado até que a Análise Final (n >= 1000) seja positiva.
*   Se positiva, o início será feito com o stake mínimo e um limite de perda diário obrigatório.
