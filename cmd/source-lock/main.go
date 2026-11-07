// source-lock validates lock structure and reports unresolved input locks.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/h2non/imaginary/internal/sourcelock"
)

func run(args []string, stdout, stderr io.Writer) int {
	jsonOutput := len(args) == 2 && args[0] == "--json"
	if jsonOutput {
		args = args[1:]
	}
	if len(args) != 1 || args[0] == "--json" {
		fmt.Fprintln(stderr, "usage: source-lock [--json] PATH")
		return 2
	}
	file, err := os.Open(args[0])
	if err != nil {
		if jsonOutput {
			writeJSON(stdout, false, nil)
		} else {
			fmt.Fprintln(stderr, "cannot open lock")
		}
		return 1
	}
	defer file.Close()
	missing, err := sourcelock.Validate(file)
	if err != nil {
		if jsonOutput {
			writeJSON(stdout, false, nil)
		} else {
			fmt.Fprintln(stderr, "invalid lock")
		}
		return 1
	}
	if jsonOutput {
		writeJSON(stdout, true, missing)
		if len(missing) > 0 {
			return 1
		}
		return 0
	}
	if len(missing) > 0 {
		for _, field := range missing {
			fmt.Fprintf(stdout, "unresolved: %s\n", field)
		}
		return 1
	}
	fmt.Fprintln(stdout, "Structure complete; authenticity verification and execution approval are separate and not implemented.")
	return 0
}

func writeJSON(w io.Writer, valid bool, unresolved []string) {
	if unresolved == nil {
		unresolved = []string{}
	}
	_ = json.NewEncoder(w).Encode(struct {
		Valid      bool     `json:"valid"`
		Unresolved []string `json:"unresolved"`
	}{valid, unresolved})
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
