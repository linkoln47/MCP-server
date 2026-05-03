package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

type Output struct {
	Files    []FileInfo    `json:"files"`
	Packages []PackageInfo `json:"packages"`
}

type FileInfo struct {
	Path        string  `json:"path"`
	Language    string  `json:"language"`
	PackageName string  `json:"package_name"`
	Chunks      []Chunk `json:"chunks"`
}

type Chunk struct {
	Kind      string `json:"kind"`
	Symbol    string `json:"symbol"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
	Content   string `json:"content"`
}

type PackageInfo struct {
	RelDir      string          `json:"rel_dir"`
	ImportPath  string          `json:"import_path"`
	PackageName string          `json:"package_name"`
	ModulePath  string          `json:"module_path"`
	Imports     []PackageImport `json:"imports"`
}

type PackageImport struct {
	ImportPath  string `json:"import_path"`
	PackageName string `json:"package_name"`
}

func main() {
	rootFlag := flag.String("root", ".", "repository root")
	flag.Parse()

	root, err := filepath.Abs(*rootFlag)
	if err != nil {
		fatal(err)
	}

	var files []FileInfo
	var packageInfos []PackageInfo
	for _, moduleRoot := range findModules(root) {
		gotFiles, gotPackages, err := loadModule(root, moduleRoot)
		if err != nil {
			fmt.Fprintf(os.Stderr, "gochunker: %s: %v\n", moduleRoot, err)
			continue
		}
		files = append(files, gotFiles...)
		packageInfos = append(packageInfos, gotPackages...)
	}

	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	sort.Slice(packageInfos, func(i, j int) bool {
		if packageInfos[i].RelDir == packageInfos[j].RelDir {
			return packageInfos[i].ImportPath < packageInfos[j].ImportPath
		}
		return packageInfos[i].RelDir < packageInfos[j].RelDir
	})

	if err := json.NewEncoder(os.Stdout).Encode(Output{Files: files, Packages: packageInfos}); err != nil {
		fatal(err)
	}
}

func findModules(root string) []string {
	var modules []string

	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && path != root && ignoredDir(d.Name()) {
			return filepath.SkipDir
		}
		if !d.IsDir() && d.Name() == "go.mod" {
			modules = append(modules, filepath.Dir(path))
		}
		return nil
	})

	if len(modules) == 0 {
		return []string{root}
	}

	sort.Strings(modules)
	return modules
}

func ignoredDir(name string) bool {
	switch name {
	case ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build", "__pycache__":
		return true
	default:
		return false
	}
}

func loadModule(repoRoot, moduleRoot string) ([]FileInfo, []PackageInfo, error) {
	cfg := &packages.Config{
		Dir:   moduleRoot,
		Tests: false,
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedModule |
			packages.NeedSyntax |
			packages.NeedTypes |
			packages.NeedTypesInfo,
	}

	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return nil, nil, err
	}

	var files []FileInfo
	var packageInfos []PackageInfo

	for _, pkg := range pkgs {
		for _, pkgErr := range pkg.Errors {
			fmt.Fprintf(os.Stderr, "gochunker: package error: %v\n", pkgErr)
		}

		packageInfos = append(packageInfos, packageInfoFromPackage(repoRoot, pkg))

		for i, file := range pkg.Syntax {
			if i >= len(pkg.CompiledGoFiles) {
				continue
			}

			absPath := pkg.CompiledGoFiles[i]
			src, err := os.ReadFile(absPath)
			if err != nil {
				continue
			}

			rel, err := filepath.Rel(repoRoot, absPath)
			if err != nil {
				continue
			}

			info := FileInfo{
				Path:        filepath.ToSlash(rel),
				Language:    "go",
				PackageName: pkg.Name,
			}

			for _, decl := range file.Decls {
				if chunk, ok := chunkFromDecl(pkg.Fset, pkg.TypesInfo, src, decl); ok {
					info.Chunks = append(info.Chunks, chunk)
				}
			}

			files = append(files, info)
		}
	}

	return files, packageInfos, nil
}

func packageInfoFromPackage(repoRoot string, pkg *packages.Package) PackageInfo {
	info := PackageInfo{
		RelDir:      packageRelDir(repoRoot, pkg),
		ImportPath:  pkg.PkgPath,
		PackageName: pkg.Name,
		ModulePath:  packageModulePath(pkg),
	}

	importPaths := make([]string, 0, len(pkg.Imports))
	for importPath := range pkg.Imports {
		importPaths = append(importPaths, importPath)
	}
	sort.Strings(importPaths)

	for _, importPath := range importPaths {
		imported := pkg.Imports[importPath]
		packageName := ""
		if imported != nil {
			packageName = imported.Name
		}

		info.Imports = append(info.Imports, PackageImport{
			ImportPath:  importPath,
			PackageName: packageName,
		})
	}

	return info
}

func packageRelDir(repoRoot string, pkg *packages.Package) string {
	for _, absPath := range append(pkg.CompiledGoFiles, pkg.GoFiles...) {
		if absPath == "" {
			continue
		}

		rel, err := filepath.Rel(repoRoot, filepath.Dir(absPath))
		if err == nil && rel != "." {
			return filepath.ToSlash(rel)
		}
		if err == nil {
			return "."
		}
	}

	return ""
}

func packageModulePath(pkg *packages.Package) string {
	if pkg.Module == nil {
		return ""
	}
	return pkg.Module.Path
}

func chunkFromDecl(fset *token.FileSet, info *types.Info, src []byte, decl ast.Decl) (Chunk, bool) {
	switch d := decl.(type) {
	case *ast.FuncDecl:
		if d.Name == nil {
			return Chunk{}, false
		}

		kind := "func"
		symbol := defName(info, d.Name)

		if d.Recv != nil && len(d.Recv.List) > 0 {
			kind = "method"
			symbol = "(" + exprName(d.Recv.List[0].Type) + ")." + symbol
		}

		return makeChunk(fset, src, declStart(decl), decl.End(), kind, symbol)

	case *ast.GenDecl:
		kind, symbol := genDeclSymbol(info, d)
		if symbol == "" {
			return Chunk{}, false
		}
		return makeChunk(fset, src, declStart(decl), decl.End(), kind, symbol)

	default:
		return Chunk{}, false
	}
}

func genDeclSymbol(info *types.Info, d *ast.GenDecl) (string, string) {
	kind := strings.ToLower(d.Tok.String())
	if kind == "import" {
		return "", ""
	}

	var names []string

	for _, spec := range d.Specs {
		switch s := spec.(type) {
		case *ast.TypeSpec:
			names = append(names, defName(info, s.Name))
			if len(d.Specs) == 1 {
				switch s.Type.(type) {
				case *ast.StructType:
					kind = "struct"
				case *ast.InterfaceType:
					kind = "interface"
				}
			}

		case *ast.ValueSpec:
			for _, name := range s.Names {
				if name.Name != "_" {
					names = append(names, defName(info, name))
				}
			}
		}
	}

	return kind, strings.Join(names, ", ")
}

func makeChunk(fset *token.FileSet, src []byte, start, end token.Pos, kind, symbol string) (Chunk, bool) {
	if !start.IsValid() || !end.IsValid() {
		return Chunk{}, false
	}

	tf := fset.File(start)
	if tf == nil {
		return Chunk{}, false
	}

	startOff := tf.Offset(start)
	endOff := tf.Offset(end)

	if startOff < 0 || startOff > len(src) || endOff < startOff {
		return Chunk{}, false
	}
	if endOff > len(src) {
		endOff = len(src)
	}
	for endOff < len(src) && src[endOff] != '\n' {
		endOff++
	}

	startPos := fset.Position(start)
	endPos := fset.Position(end)

	return Chunk{
		Kind:      kind,
		Symbol:    symbol,
		StartLine: startPos.Line,
		EndLine:   endPos.Line,
		Content:   strings.TrimRight(string(src[startOff:endOff]), "\n"),
	}, true
}

func declStart(decl ast.Decl) token.Pos {
	switch d := decl.(type) {
	case *ast.FuncDecl:
		if d.Doc != nil {
			return d.Doc.Pos()
		}
	case *ast.GenDecl:
		if d.Doc != nil {
			return d.Doc.Pos()
		}
	}
	return decl.Pos()
}

func defName(info *types.Info, ident *ast.Ident) string {
	if ident == nil {
		return ""
	}
	if info != nil {
		if obj := info.Defs[ident]; obj != nil {
			return obj.Name()
		}
	}
	return ident.Name
}

func exprName(expr ast.Expr) string {
	switch x := expr.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.StarExpr:
		return "*" + exprName(x.X)
	case *ast.SelectorExpr:
		return exprName(x.X) + "." + x.Sel.Name
	case *ast.IndexExpr:
		return exprName(x.X)
	case *ast.IndexListExpr:
		return exprName(x.X)
	case *ast.ParenExpr:
		return exprName(x.X)
	default:
		return "receiver"
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
