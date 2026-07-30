# Static AST checks for the recurring PowerShell bug classes.
#
# Emits one line per violation: TYPE|line|detail
#   PARSE_ERROR          - the file does not parse (exit code 2)
#   SCRIPT_SCOPE_RETURN  - `return` outside any function/scriptblock; at script scope it
#                          always exits 0 and discards the errTotal -> exit 1 contract
#   MISSING_INJECTION    - a ForEach-Object -Parallel block calls a script-level function
#                          without re-injecting it via ${function:Name} = $using:...FnDef
#                          (functions from the parent scope are invisible in runspaces)
#
# Consumed by tests/test_regression_classes.py. Runs under both PowerShell 5.1 and 7.
param([Parameter(Mandatory = $true)][string]$Path)

$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)

if ($errors -and @($errors).Count -gt 0) {
    foreach ($e in $errors) {
        Write-Output "PARSE_ERROR|$($e.Extent.StartLineNumber)|$($e.Message)"
    }
    exit 2
}

# -- Script-level function definitions ------------------------------
$funcs = @{}
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    $funcs[$f.Name] = $f
}

# -- `return` at script scope ---------------------------------------
foreach ($r in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ReturnStatementAst] }, $true)) {
    $p = $r.Parent
    $scoped = $false
    while ($p) {
        if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
            $p -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -or
            $p -is [System.Management.Automation.Language.TrapStatementAst]) {
            $scoped = $true
            break
        }
        $p = $p.Parent
    }
    if (-not $scoped) {
        Write-Output "SCRIPT_SCOPE_RETURN|$($r.Extent.StartLineNumber)|$($r.Extent.Text)"
    }
}

# -- Helper re-injection inside -Parallel runspaces -----------------
function Get-CalledFunctionName {
    param($Node, $Known)
    $names = New-Object System.Collections.ArrayList
    foreach ($c in $Node.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $n = $c.GetCommandName()
        if ($n -and $Known.ContainsKey($n)) { [void]$names.Add($n) }
    }
    return $names
}

$parallelBlocks = New-Object System.Collections.ArrayList
foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    if ($c.GetCommandName() -ne 'ForEach-Object') { continue }
    $isParallel = $false
    foreach ($el in $c.CommandElements) {
        if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'Parallel') {
            $isParallel = $true
            if ($el.Argument -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
                [void]$parallelBlocks.Add($el.Argument)
            }
        }
    }
    if (-not $isParallel) { continue }
    foreach ($el in $c.CommandElements) {
        if ($el -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
            [void]$parallelBlocks.Add($el)
        }
    }
}

foreach ($block in $parallelBlocks) {
    $injected = @{}
    foreach ($v in $block.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
        $up = $v.VariablePath.UserPath
        if ($up -like 'function:*') {
            $injected[$up.Substring('function:'.Length)] = $true
        }
    }

    # Transitive closure: an injected helper that calls another script-level function
    # needs that one injected too (Test-ZipIntegrity -> Invoke-MagickWithTimeout).
    $needed = @{}
    $queue = New-Object System.Collections.Queue
    foreach ($n in (Get-CalledFunctionName -Node $block -Known $funcs)) { $queue.Enqueue($n) }
    while ($queue.Count -gt 0) {
        $n = $queue.Dequeue()
        if ($needed.ContainsKey($n)) { continue }
        $needed[$n] = $true
        foreach ($m in (Get-CalledFunctionName -Node $funcs[$n] -Known $funcs)) {
            if (-not $needed.ContainsKey($m)) { $queue.Enqueue($m) }
        }
    }

    foreach ($n in ($needed.Keys | Sort-Object)) {
        if (-not $injected.ContainsKey($n)) {
            Write-Output "MISSING_INJECTION|$($block.Extent.StartLineNumber)|$n"
        }
    }
}

exit 0
