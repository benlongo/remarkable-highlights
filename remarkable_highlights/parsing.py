from shapely.geometry import CAP_STYLE, JOIN_STYLE, LineString

# NOTE: This is indeed, supposed to be a string
# we care about the precision and don't wish to faff about w/ floats
YELLOW = ("1", "0.952941", "0.658824")  # RGB


def content_contains_highlight(content):
    return "{} {} {} RG".format(*YELLOW).encode("utf-8") in content


# All 'Path Construction Operators'
# section 8.5.2 from https://www.adobe.com/content/dam/acom/en/devnet/pdf/PDF32000_2008.pdf
PATH_OPS = [
    "w",
    "J",
    "j",
    "M",
    "d",
    "ri",
    "i",
    "gs",
    "q",
    "Q",
    "cm",
    "m",
    "l",
    "c",
    "v",
    "y",
    "h",
    "re",
    "S",
    "s",
    "f",
    "F",
    "f*",
    "B",
    "B*",
    "b",
    "b*",
    "n",
    "W",
    "W*",
    "BT",
    "ET",
    "Tc",
    "Tw",
    "Tz",
    "TL",
    "Tf",
    "Tr",
    "Ts",
    "i",
    "Td",
    "TD",
    "Tm",
    "T*",
    "Tj",
    "TJ",
    "'",
    '"',
    "d0",
    "d1",
    "CS",
    "cs",
    "SC",
    "SCN",
    "sc",
    "scn",
    "G",
    "g",
    "RG",
    "rg",
    "K",
    "k",
    "sh",
    "BI",
    "ID",
    "EI",
    "Do",
    "MP",
    "DP",
    "BMC",
    "BDC",
    "EMC",
    "BX",
    "EX",
]


def tokenize_graphics(raw_graphics_content):
    """Convert a raw graphics content stream into a sequence of operations and their arguments.

    PDF operations are in postfix position, so the easiest way to parse them is to use a stack.
    Arguments are added onto the stack until we see a valid operation code. When we see a valid
    operation, we pop all the arguments off of the stack and continue onto the next operation.

    To be safe, all Path Construction Operations have been included in PATH_OPS. However, there
    are indeed more operations defined in the PDF specification, and no effort is made to ensure
    that the arguments to the operations are valid.
    """
    arg_stack = []
    for token in raw_graphics_content.decode("utf-8").split():
        if token in PATH_OPS:
            yield (token, arg_stack.copy())
            arg_stack.clear()
        else:
            arg_stack.append(token)


# It so happens that these numbers are the same, but we shant rely on that remaining the same!
# This also is a nice way of documenting what the numbers actually mean.
CAP_STYLES = {1: CAP_STYLE.round, 2: CAP_STYLE.flat, 3: CAP_STYLE.square}

JOIN_STYLES = {1: JOIN_STYLE.round, 2: JOIN_STYLE.mitre, 3: JOIN_STYLE.bevel}


def highlighter_lines(raw_graphics_content):
    """Render all highlighter lines into LineStrings.

    This function implements a (very) small subset of the PDF specification for drawing paths.
    Individual highlighter strokes are rendered into shapely LineString objects. Luckily for us,
    and almost certainly not due to coincidence, the semantics for defining LineString objects is
    nearly identical those of PDF paths.

    Here's an annotated example of a highlighter path pulled from a PDF:

    q                         -- push current graphics state onto the stack
    1 0.952941 0.658824 RG    -- set the stroking color to use in RGB
    12.5480766 w              -- set the line width (total width, not from centerline)
    1 J                       -- set the line cap style to 'round'
    1 j                       -- set the line join style to 'round'
    /FXE2 gs                  -- no idea what this does
    1 0 0 1 0 0 cm            -- set the current transformation matrix
    0 0 m                     -- move the cursor without drawing a line
    397.95938 614.77747 m     -- move the cursor again, overriding the previous value
    397.48959 615.17413 l     -- add a straight line segment from the current point to this point
    396.60797 615.76379 l     -- do that many, many more times
    ...                       -- and a lot more times
    51.715569 609.7724 l      -- last time
    S                         -- stroke the path
    Q                         -- pop from the graphics state stack to restore the previous value

    Key assumptions:
      - the specified color is always exactly the same (even the precision)
      - the 'RG' operation setting the color appears before all other relevant operations
      - only straight lines are used (no curves!), this isn't the case for other markers
      - the transformation matrix (if specified) is always the identity matrix
        this is verified however, so if this assumption is violated there will be an error
    """
    lines = []

    current_line = []
    width = None
    cap_style = None
    join_style = None

    for op, args in tokenize_graphics(raw_graphics_content):
        if (op, args) == ("RG", list(YELLOW)):
            current_line = [None]  # To be overrode by the last m op
        elif current_line:
            if op == "m":  # Move the draw position
                current_line[0] = (float(args[0]), float(args[1]))
            elif op == "j":
                i_join_style = int(args[0])
                join_style = JOIN_STYLES[i_join_style]
            elif op == "J":
                i_cap_style = int(args[0])
                cap_style = CAP_STYLES[i_cap_style]
            elif op == "w":
                width = float(args[0])  # Total width, will /2 later
            elif op == "l":
                current_line.append((float(args[0]), float(args[1])))
            elif op == "S":  # Finish the line
                # Make sure we got all the params we need to draw the line correctly
                assert (
                    width is not None
                ), "Expected to see a width for the stroke before stroke end."
                assert (
                    cap_style is not None
                ), "Expected to see a cap style before stroke end."
                assert (
                    join_style is not None
                ), "Expected to see a join style before stroke end."
                assert len(current_line) > 1, "Invalid line, not enough points."

                # Draw that thang
                yield LineString(current_line).buffer(
                    width / 2, cap_style=cap_style, join_style=join_style
                )

                # Reset the state
                current_line = None
                width = None
                cap_style = None
                join_style = None
            elif op == "cm":
                if args != ["1", "0", "0", "1", "0", "0"]:
                    raise NotImplementedError(
                        "Transform matrices are not implemented, but shouldn't be hard to implement"
                    )
            else:
                pass  # We don't care about other operations
