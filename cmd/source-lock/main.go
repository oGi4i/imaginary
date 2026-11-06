// source-lock validates lock structure and reports unresolved input locks.
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/h2non/imaginary/internal/sourcelock"
)

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) != 1 {
		fmt.Fprintln(stderr, "usage: source-lock PATH")
		return 2
	}
	file, err := os.Open(args[0])
	if err != nil {
		fmt.Fprintln(stderr, "cannot open lock")
		return 1
	}
	defer file.Close()
	missing, err := sourcelock.Validate(file)
	if err != nil {
		fmt.Fprintln(stderr, "invalid lock")
		return 1
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

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
