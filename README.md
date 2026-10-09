# DiagCompleto

Diagnóstico de saúde e otimizador para Windows 10/11, feito para **deixar o sistema limpo e leve sem
correr riscos**.

- Escrito em Python usando só a biblioteca padrão (tkinter, winreg, ctypes), **sem dependências externas**.
- Usa as ferramentas oficiais do próprio Windows: PowerShell/CIM, DISM, SFC e powercfg.
- Toda otimização tem backup e pode ser revertida.

## Sumário

- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Como usar](#como-usar)
- [Recursos](#recursos)
- [Regras de segurança](#regras-de-segurança)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Testes](#testes)

## Requisitos

- Windows 10 ou 11
- Python 3.10 ou superior
- Permissão de administrador (opcional, mas recomendada; veja abaixo)

## Instalação

```bat
git clone https://github.com/gabedossa/Diagpc.git
cd Diagpc
```

Não há nada para instalar: basta ter o Python.

> **Importante:** o `Executar.bat` roda como administrador o código que está na pasta do programa.
> Coloque essa pasta num local que só administradores podem alterar, como
> `C:\Program Files\DiagCompleto`. Se ela ficar em Documentos ou Downloads, qualquer programa que
> rode como você pode editar os arquivos `.py` e esperar você abrir o DiagCompleto como administrador.
>
> Para rodar como administrador (pelo `Executar.bat` ou pelo botão "Executar como admin"), o Python
> precisa estar instalado **para todos os usuários**, em Program Files (opção *Install for all users*
> do instalador). O interpretador é localizado pelo registro `HKLM`, nunca pelo `PATH`, que qualquer
> programa rodando como você consegue alterar. Sem essa instalação, o programa avisa e não eleva.

## Como usar

### Interface gráfica

Dê dois cliques em **`Executar.bat`**. Ele abre o programa pedindo permissão de administrador.

A interface abre no tema escuro. Para trocar, use **Tema claro / Tema escuro** no rodapé da barra lateral; a escolha fica salva.

Para abrir sem elevação, use `python main.py`.

### Linha de comando

| Comando | O que faz |
| --- | --- |
| `python main.py saude` | Diagnóstico de saúde no terminal |
| `python main.py saude --html` | Diagnóstico + relatório HTML aberto no navegador |
| `python main.py analisar` | Espaço recuperável por categoria |
| `python main.py limpar` | Limpa as categorias recomendadas (pede confirmação) |
| `python main.py limpar -y` | Limpa sem pedir confirmação |
| `python main.py limpar <ids>` | Limpa só as categorias indicadas |
| `python main.py ajustes` | Estado de cada otimização |
| `python main.py inicializacao` | Programas que iniciam com o Windows |
| `python main.py --help` | Lista todos os comandos |

### Sem administrador

O programa funciona sem administrador, mas com limitações:

- não lê alguns sensores (temperatura, desgaste do SSD, capacidade original da bateria);
- desativa as ações que mexem no sistema.

## Recursos

| Aba | O que faz |
| --- | --- |
| **Saúde do PC** | Analisa discos (saúde, desgaste, temperatura, erros, previsão S.M.A.R.T.), espaço livre, RAM e módulos, CPU e temperatura, placas de vídeo e idade do driver, e bateria (capacidade vs. original e ciclos). Verifica também erros de hardware WHEA, telas azuis, desligamentos inesperados, erros de disco, dispositivos com problema, antivírus e reinicialização pendente. Dá uma nota de 0 a 100 e exporta relatório HTML. |
| **Limpeza** | Temporários, cache de miniaturas, cache de navegadores, relatórios de erro, dumps, logs antigos, cache de shaders, cache do Windows Update e Lixeira. Mostra o tamanho antes de limpar. |
| **Otimizações** | Ajustes de privacidade, interface e desempenho, todos com backup e botão "Reverter". |
| **Inicialização** | Ativa e desativa programas de inicialização. |
| **Manutenção** | DISM (limpeza de componentes, análise, reparo), SFC, Otimizar Unidades, limpar DNS e criar ponto de restauração. |

## Regras de segurança

### Limpeza

- Só apaga dentro de uma lista fechada de pastas de cache e temporários. Raízes perigosas (disco,
  Windows, System32, perfil do usuário, Program Files…) são recusadas mesmo que apareçam por engano.
- Nunca segue links simbólicos nem junções, então não há como "escapar" para outra pasta.
- Temporários com menos de 24 h são preservados. Vale a data mais recente entre criação e modificação,
  o que protege instaladores em andamento. Arquivos em uso são ignorados.
- Nos navegadores, apaga apenas o cache. Senhas, cookies, histórico e extensões nunca são tocados.
- Itens com efeito colateral (Lixeira, dumps de tela azul, cache de shaders, Windows Update) vêm
  desmarcados por padrão.

### Otimizações

- Antes de qualquer mudança, o valor original de cada chave é salvo em `tweaks_backup.json`
  (veja [Arquivos gerados](#arquivos-gerados)).
- Reverter restaura exatamente o estado anterior, inclusive apagando valores que não existiam.
  Reaplicar nunca sobrescreve o backup original.
- O backup é conferido antes de ser restaurado: chave e nome vêm sempre da definição do ajuste,
  e tipo e valor precisam ser válidos. Um backup adulterado é recusado e nada é alterado.
- Há a opção de criar um ponto de restauração do Windows antes de aplicar.
- Programas de inicialização usam o mesmo mecanismo do Gerenciador de Tarefas (`StartupApproved`).
  Nada é apagado.

### Proteção quando roda como administrador

O programa parte do princípio de que outro software rodando como o usuário (sem elevação) pode
tentar usá-lo para ganhar privilégios. Por isso, quando elevado:

- **Pastas vêm das APIs do Windows**, nunca de variáveis como `%TEMP%` ou `%SystemRoot%`, que o
  usuário pode apontar para qualquer lugar. A limpeza só age dentro do perfil do usuário, da pasta
  do Windows e do ProgramData, e recusa raízes que passem por junção ou link.
- **Cada exclusão é conferida pelo handle**: o arquivo é aberto, o caminho real é verificado e só
  então ele é apagado. Trocar uma subpasta por junção no meio da limpeza não leva a exclusão para
  fora da pasta.
- **Ferramentas do sistema são chamadas por caminho completo** (PowerShell, DISM, SFC, powercfg),
  com `PATH` e `PSModulePath` restritos às pastas do Windows. Um `powershell.exe` ou um módulo
  plantado em pastas do usuário não é executado.
- **Dados ficam em `%ProgramData%\DiagCompleto`**, uma pasta criada já protegida, que só
  administradores alteram. Se ela existir sem essa proteção, é renomeada e recriada.
- **Relatórios abrem no navegador sem elevação**, via Explorador.

### O que o DiagCompleto deliberadamente **não** faz

Coisas comuns em "otimizadores", mas arriscadas ou inúteis:

- **"Limpeza de registro"**: ganho nulo e risco real de quebrar programas.
- **Limpar a pasta Prefetch**: deixa a abertura de programas *mais lenta*.
- **Desativar Windows Update, Windows Defender, SysMain ou arquivo de paginação.**
- **`DISM /ResetBase`**: impediria desinstalar atualizações problemáticas.
- **Remover drivers ou apps do sistema.**

## Estrutura do projeto

```text
main.py                       ponto de entrada (GUI sem argumentos, CLI com argumentos)
Executar.bat                  abre a GUI como administrador
diagcompleto/
├── health.py                 coleta (PowerShell/CIM, ctypes) e avaliação da saúde
├── cleaner.py                alvos de limpeza e regras de segurança
├── tweaks.py                 otimizações reversíveis e backup
├── startup.py                programas de inicialização
├── maintenance.py            DISM, SFC, otimização de unidades, ponto de restauração
├── report.py                 relatório HTML
├── utils.py                  privilégios, execução de comandos, formatação e logging
├── winsec.py                 pastas confiáveis, exclusão segura e pasta protegida (modo admin)
├── gui.py                    interface tkinter
└── cli.py                    linha de comando
tests/                        testes (pytest)
```

### Arquivos gerados

A pasta depende de como o programa foi aberto:

| Aberto como | Pasta de dados |
| --- | --- |
| Administrador | `%ProgramData%\DiagCompleto` (protegida) |
| Usuário comum | `%LOCALAPPDATA%\DiagCompleto` |

Dentro dela ficam `logs\`, `relatorios\`, `tweaks_backup.json` e `settings.json` (tema escolhido). Como administrador, um backup antigo
em `%LOCALAPPDATA%` ainda é lido (e conferido), mas nunca alterado.

## Testes

```bat
pip install pytest
python -m pytest
```

Os testes não mexem no seu sistema:

- os de limpeza usam pastas temporárias;
- os de otimização usam uma chave de teste própria, `HKCU\Software\DiagCompletoTest`, removida ao final;
- os de segurança (`tests/test_security.py`) simulam os ataques: junção trocada no meio da limpeza,
  `%TEMP%` desviado, backup adulterado, módulo do PowerShell plantado e ACL fraca.
